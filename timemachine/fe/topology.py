from typing import List

import jax.numpy as jnp
import numpy as np
from numpy.typing import NDArray

from timemachine.fe import chiral_utils
from timemachine.fe.system import VacuumSystem
from timemachine.fe.utils import get_romol_conf
from timemachine.ff.handlers import nonbonded
from timemachine.lib import potentials
from timemachine.potentials.nonbonded import combining_rule_epsilon, combining_rule_sigma

_SCALE_12 = 1.0
_SCALE_13 = 1.0
_SCALE_14 = 0.5
_BETA = 2.0
_CUTOFF = 1.2


class AtomMappingError(Exception):
    pass


class UnsupportedPotential(Exception):
    pass


class HostGuestTopology:
    def __init__(self, host_potentials, guest_topology):
        """
        Utility tool for combining host with a guest, in that order. host_potentials must be comprised
        exclusively of supported potentials (currently: bonds, angles, torsions, nonbonded).

        Parameters
        ----------
        host_potentials:
            Bound potentials for the host.

        guest_topology:
            Guest's Topology {Base, Dual, Single}Topology.

        """
        self.guest_topology = guest_topology

        self.host_nonbonded = None
        self.host_harmonic_bond = None
        self.host_harmonic_angle = None
        self.host_periodic_torsion = None

        # (ytz): extra assertions inside are to ensure we don't have duplicate terms
        for bp in host_potentials:
            if isinstance(bp, potentials.HarmonicBond):
                assert self.host_harmonic_bond is None
                self.host_harmonic_bond = bp
            elif isinstance(bp, potentials.HarmonicAngle):
                assert self.host_harmonic_angle is None
                self.host_harmonic_angle = bp
            elif isinstance(bp, potentials.PeriodicTorsion):
                assert self.host_periodic_torsion is None
                self.host_periodic_torsion = bp
            elif isinstance(bp, potentials.Nonbonded):
                assert self.host_nonbonded is None
                self.host_nonbonded = bp
            else:
                raise UnsupportedPotential("Unsupported host potential")

        self.num_host_atoms = len(self.host_nonbonded.get_lambda_plane_idxs())

    def get_num_atoms(self):
        return self.num_host_atoms + self.guest_topology.get_num_atoms()

    def get_component_idxs(self) -> List[NDArray]:
        """
        Return the atom indices for each component of
        this topology as a list of NDArray. If the host is
        not present, this will just be the result from the guest topology.
        Otherwise, the result is in the order host atom idxs then guest
        component atom idxs.
        """
        host_idxs = [np.arange(self.num_host_atoms)] if self.num_host_atoms else []
        guest_idxs = [
            guest_component_idxs + self.num_host_atoms
            for guest_component_idxs in self.guest_topology.get_component_idxs()
        ]
        return host_idxs + guest_idxs

    # tbd: just merge the hamiltonians here
    def _parameterize_bonded_term(self, guest_params, guest_potential, host_potential):

        if guest_potential is None:
            raise UnsupportedPotential("Mismatch in guest_potential")

        # (ytz): corner case exists if the guest_potential is None
        if host_potential is not None:
            assert type(host_potential) == type(guest_potential)

        guest_idxs = guest_potential.get_idxs() + self.num_host_atoms

        guest_lambda_mult = guest_potential.get_lambda_mult()
        guest_lambda_offset = guest_potential.get_lambda_offset()

        if guest_lambda_mult is None:
            guest_lambda_mult = np.zeros(len(guest_params))
        if guest_lambda_offset is None:
            guest_lambda_offset = np.ones(len(guest_params))

        if host_potential is not None:
            # the host is always on.
            host_params = host_potential.params
            host_idxs = host_potential.get_idxs()
            host_lambda_mult = np.zeros(len(host_idxs), dtype=np.int32)
            host_lambda_offset = np.ones(len(host_idxs), dtype=np.int32)
        else:
            # (ytz): this extra jank is to work around jnp.concatenate not supporting empty lists.
            host_params = np.array([], dtype=guest_params.dtype).reshape((-1, guest_params.shape[1]))
            host_idxs = np.array([], dtype=guest_idxs.dtype).reshape((-1, guest_idxs.shape[1]))
            host_lambda_mult = []
            host_lambda_offset = []

        combined_params = jnp.concatenate([host_params, guest_params])
        combined_idxs = np.concatenate([host_idxs, guest_idxs])
        combined_lambda_mult = np.concatenate([host_lambda_mult, guest_lambda_mult]).astype(np.int32)
        combined_lambda_offset = np.concatenate([host_lambda_offset, guest_lambda_offset]).astype(np.int32)

        ctor = type(guest_potential)

        return combined_params, ctor(combined_idxs, combined_lambda_mult, combined_lambda_offset)

    def parameterize_harmonic_bond(self, ff_params):
        guest_params, guest_potential = self.guest_topology.parameterize_harmonic_bond(ff_params)
        return self._parameterize_bonded_term(guest_params, guest_potential, self.host_harmonic_bond)

    def parameterize_harmonic_angle(self, ff_params):
        guest_params, guest_potential = self.guest_topology.parameterize_harmonic_angle(ff_params)
        return self._parameterize_bonded_term(guest_params, guest_potential, self.host_harmonic_angle)

    def parameterize_periodic_torsion(self, proper_params, improper_params):
        guest_params, guest_potential = self.guest_topology.parameterize_periodic_torsion(
            proper_params, improper_params
        )
        return self._parameterize_bonded_term(guest_params, guest_potential, self.host_periodic_torsion)

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):
        num_guest_atoms = self.guest_topology.get_num_atoms()
        guest_qlj, guest_p = self.guest_topology.parameterize_nonbonded(ff_q_params, ff_lj_params)

        if isinstance(guest_p, potentials.NonbondedInterpolated):
            assert guest_qlj.shape[0] == num_guest_atoms * 2
            is_interpolated = True
        else:
            assert guest_qlj.shape[0] == num_guest_atoms
            is_interpolated = False

        # see if we're doing parameter interpolation
        assert guest_qlj.shape[1] == 3
        assert guest_p.get_beta() == self.host_nonbonded.get_beta()
        assert guest_p.get_cutoff() == self.host_nonbonded.get_cutoff()

        hg_exclusion_idxs = np.concatenate(
            [self.host_nonbonded.get_exclusion_idxs(), guest_p.get_exclusion_idxs() + self.num_host_atoms]
        )
        hg_scale_factors = np.concatenate([self.host_nonbonded.get_scale_factors(), guest_p.get_scale_factors()])
        hg_lambda_offset_idxs = np.concatenate(
            [self.host_nonbonded.get_lambda_offset_idxs(), guest_p.get_lambda_offset_idxs()]
        )
        hg_lambda_plane_idxs = np.concatenate(
            [self.host_nonbonded.get_lambda_plane_idxs(), guest_p.get_lambda_plane_idxs()]
        )

        if is_interpolated:
            # with parameter interpolation
            hg_nb_params_src = jnp.concatenate([self.host_nonbonded.params, guest_qlj[:num_guest_atoms]])
            hg_nb_params_dst = jnp.concatenate([self.host_nonbonded.params, guest_qlj[num_guest_atoms:]])
            hg_nb_params = jnp.concatenate([hg_nb_params_src, hg_nb_params_dst])

            nb = potentials.NonbondedInterpolated(
                hg_exclusion_idxs,
                hg_scale_factors,
                hg_lambda_plane_idxs,
                hg_lambda_offset_idxs,
                guest_p.get_beta(),
                guest_p.get_cutoff(),
            )

            return hg_nb_params, nb
        else:
            # no parameter interpolation
            hg_nb_params = jnp.concatenate([self.host_nonbonded.params, guest_qlj])

            return hg_nb_params, potentials.Nonbonded(
                hg_exclusion_idxs,
                hg_scale_factors,
                hg_lambda_plane_idxs,
                hg_lambda_offset_idxs,
                guest_p.get_beta(),
                guest_p.get_cutoff(),
            )


class BaseTopology:
    def __init__(self, mol, forcefield):
        """
        Utility for working with a single ligand.

        Parameter
        ---------
        mol: ROMol
            Ligand to be parameterized

        forcefield: ff.Forcefield
            A convenience wrapper for forcefield lists.

        """
        self.mol = mol
        self.ff = forcefield

    def get_num_atoms(self):
        return self.mol.GetNumAtoms()

    def get_component_idxs(self) -> List[NDArray]:
        """
        Return the atom indices for the molecule in
        this topology as a list of NDArray.
        """
        return [np.arange(self.get_num_atoms())]

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):
        q_params = self.ff.q_handle.partial_parameterize(ff_q_params, self.mol)
        lj_params = self.ff.lj_handle.partial_parameterize(ff_lj_params, self.mol)

        exclusion_idxs, scale_factors = nonbonded.generate_exclusion_idxs(
            self.mol, scale12=_SCALE_12, scale13=_SCALE_13, scale14=_SCALE_14
        )

        scale_factors = np.stack([scale_factors, scale_factors], axis=1)

        N = len(q_params)

        lambda_plane_idxs = np.zeros(N, dtype=np.int32)
        lambda_offset_idxs = np.ones(N, dtype=np.int32)

        beta = _BETA
        cutoff = _CUTOFF  # solve for this analytically later

        nb = potentials.Nonbonded(exclusion_idxs, scale_factors, lambda_plane_idxs, lambda_offset_idxs, beta, cutoff)

        params = jnp.concatenate([jnp.reshape(q_params, (-1, 1)), jnp.reshape(lj_params, (-1, 2))], axis=1)

        return params, nb

    def parameterize_nonbonded_pairlist(self, ff_q_params, ff_lj_params):
        """
        Generate intramolecular nonbonded pairlist, and is mostly identical to the above
        except implemented as a pairlist.
        """
        # use same scale factors for electrostatics and vdWs
        exclusion_idxs, scale_factors = nonbonded.generate_exclusion_idxs(
            self.mol, scale12=_SCALE_12, scale13=_SCALE_13, scale14=_SCALE_14
        )

        # note: use same scale factor for electrostatics and vdw
        # typically in protein ffs, gaff, the 1-4 ixns use different scale factors between vdw and electrostatics
        exclusions_kv = dict()
        for (i, j), sf in zip(exclusion_idxs, scale_factors):
            assert i < j
            exclusions_kv[(i, j)] = sf

        # loop over all pairs
        inclusion_idxs, rescale_mask = [], []
        for i in range(self.mol.GetNumAtoms()):
            for j in range(i + 1, self.mol.GetNumAtoms()):
                scale_factor = exclusions_kv.get((i, j), 0.0)
                rescale_factor = 1 - scale_factor
                # keep this ixn
                if rescale_factor > 0:
                    rescale_mask.append([rescale_factor, rescale_factor])
                    inclusion_idxs.append([i, j])

        inclusion_idxs = np.array(inclusion_idxs).reshape(-1, 2).astype(np.int32)

        q_params = self.ff.q_handle.partial_parameterize(ff_q_params, self.mol)
        lj_params = self.ff.lj_handle.partial_parameterize(ff_lj_params, self.mol)

        sig_params = lj_params[:, 0]
        eps_params = lj_params[:, 1]

        l_idxs = inclusion_idxs[:, 0]
        r_idxs = inclusion_idxs[:, 1]

        q_ij = q_params[l_idxs] * q_params[r_idxs]
        sig_ij = combining_rule_sigma(sig_params[l_idxs], sig_params[r_idxs])
        eps_ij = combining_rule_epsilon(eps_params[l_idxs], eps_params[r_idxs])

        params = []
        for q, s, e, (sf_q, sf_lj) in zip(q_ij, sig_ij, eps_ij, rescale_mask):
            params.append((q * sf_q, s, e * sf_lj))

        params = np.array(params)

        beta = _BETA
        cutoff = _CUTOFF  # solve for this analytically later

        offsets = np.zeros(len(inclusion_idxs))

        return params, potentials.NonbondedPairListPrecomputed(inclusion_idxs, offsets, beta, cutoff)

    def parameterize_harmonic_bond(self, ff_params):
        params, idxs = self.ff.hb_handle.partial_parameterize(ff_params, self.mol)
        return params, potentials.HarmonicBond(idxs)

    def parameterize_harmonic_angle(self, ff_params):
        params, idxs = self.ff.ha_handle.partial_parameterize(ff_params, self.mol)
        return params, potentials.HarmonicAngle(idxs)

    def parameterize_proper_torsion(self, ff_params):
        params, idxs = self.ff.pt_handle.partial_parameterize(ff_params, self.mol)
        return params, potentials.PeriodicTorsion(idxs)

    def parameterize_improper_torsion(self, ff_params):
        params, idxs = self.ff.it_handle.partial_parameterize(ff_params, self.mol)
        return params, potentials.PeriodicTorsion(idxs)

    def parameterize_periodic_torsion(self, proper_params, improper_params):
        """
        Parameterize all periodic torsions in the system.
        """
        proper_params, proper_potential = self.parameterize_proper_torsion(proper_params)
        improper_params, improper_potential = self.parameterize_improper_torsion(improper_params)
        combined_params = jnp.concatenate([proper_params, improper_params])
        combined_idxs = np.concatenate([proper_potential.get_idxs(), improper_potential.get_idxs()])

        proper_lambda_mult = proper_potential.get_lambda_mult()
        proper_lambda_offset = proper_potential.get_lambda_offset()

        if proper_lambda_mult is None:
            proper_lambda_mult = np.zeros(len(proper_params))
        if proper_lambda_offset is None:
            proper_lambda_offset = np.ones(len(proper_params))

        improper_lambda_mult = improper_potential.get_lambda_mult()
        improper_lambda_offset = improper_potential.get_lambda_offset()

        if improper_lambda_mult is None:
            improper_lambda_mult = np.zeros(len(improper_params))
        if improper_lambda_offset is None:
            improper_lambda_offset = np.ones(len(improper_params))

        combined_lambda_mult = np.concatenate([proper_lambda_mult, improper_lambda_mult]).astype(np.int32)
        combined_lambda_offset = np.concatenate([proper_lambda_offset, improper_lambda_offset]).astype(np.int32)

        combined_potential = potentials.PeriodicTorsion(combined_idxs, combined_lambda_mult, combined_lambda_offset)
        return combined_params, combined_potential

    # def setup_chiral_restraints(self, restraint_k=1000.0):
    def setup_chiral_restraints(self, restraint_k=1000.0):
        """
        Create chiral atom and bond potentials.

        Parameters
        ----------
        restraint_k: float
            Force constant of the restraints

        Returns
        -------
        2-tuple
            Returns a ChiralAtomRestraint and a ChiralBondRestraint

        """
        chiral_atoms = chiral_utils.find_chiral_atoms(self.mol)
        chiral_bonds = chiral_utils.find_chiral_bonds(self.mol)

        chiral_atom_restr_idxs = []
        chiral_atom_params = []
        for a_idx in chiral_atoms:
            idxs = chiral_utils.setup_chiral_atom_restraints(self.mol, get_romol_conf(self.mol), a_idx)
            for ii in idxs:
                assert ii not in chiral_atom_restr_idxs
            chiral_atom_restr_idxs.extend(idxs)
            chiral_atom_params.extend(restraint_k for _ in idxs)

        chiral_atom_params = np.array(chiral_atom_params)
        chiral_atom_restr_idxs = np.array(chiral_atom_restr_idxs)
        chiral_atom_potential = potentials.ChiralAtomRestraint(chiral_atom_restr_idxs).bind(chiral_atom_params)

        chiral_bond_restr_idxs = []
        chiral_bond_restr_signs = []
        chiral_bond_params = []
        for src_idx, dst_idx in chiral_bonds:
            idxs, signs = chiral_utils.setup_chiral_bond_restraints(
                self.mol, get_romol_conf(self.mol), src_idx, dst_idx
            )
            for ii in idxs:
                assert ii not in chiral_bond_restr_idxs
            chiral_bond_restr_idxs.extend(idxs)
            chiral_bond_restr_signs.extend(signs)
            chiral_bond_params.extend(restraint_k for _ in idxs)

        chiral_bond_restr_idxs = np.array(chiral_bond_restr_idxs)
        chiral_bond_restr_signs = np.array(chiral_bond_restr_signs)
        chiral_bond_params = np.array(chiral_bond_params)
        chiral_bond_potential = potentials.ChiralBondRestraint(chiral_bond_restr_idxs, chiral_bond_restr_signs).bind(
            chiral_bond_params
        )

        return chiral_atom_potential, chiral_bond_potential

    def setup_chiral_end_state(self):
        """
        Setup an end-state with chiral restraints attached.
        """
        system = self.setup_end_state()
        chiral_atom_potential, chiral_bond_potential = self.setup_chiral_restraints()
        system.chiral_atom = chiral_atom_potential
        system.chiral_bond = chiral_bond_potential
        return system

    def setup_end_state(self):
        mol_bond_params, mol_hb = self.parameterize_harmonic_bond(self.ff.hb_handle.params)
        mol_angle_params, mol_ha = self.parameterize_harmonic_angle(self.ff.ha_handle.params)
        mol_proper_params, mol_pt = self.parameterize_proper_torsion(self.ff.pt_handle.params)
        mol_improper_params, mol_it = self.parameterize_improper_torsion(self.ff.it_handle.params)
        mol_nbpl_params, mol_nbpl = self.parameterize_nonbonded_pairlist(
            self.ff.q_handle.params, self.ff.lj_handle.params
        )
        bond_potential = mol_hb.bind(mol_bond_params)
        angle_potential = mol_ha.bind(mol_angle_params)

        torsion_params = np.concatenate([mol_proper_params, mol_improper_params])
        torsion_idxs = np.concatenate([mol_pt.get_idxs(), mol_it.get_idxs()])
        torsion_potential = potentials.PeriodicTorsion(torsion_idxs).bind(torsion_params)
        nonbonded_potential = mol_nbpl.bind(mol_nbpl_params)

        system = VacuumSystem(bond_potential, angle_potential, torsion_potential, nonbonded_potential, None, None)

        return system


class BaseTopologyConversion(BaseTopology):
    """
    Decharges the ligand and reduces the LJ epsilon by half.
    """

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):

        qlj_params, nb_potential = super().parameterize_nonbonded(ff_q_params, ff_lj_params)
        charge_indices = jnp.index_exp[:, 0]
        epsilon_indices = jnp.index_exp[:, 2]

        src_qlj_params = qlj_params
        dst_qlj_params = jnp.asarray(qlj_params).at[charge_indices].set(0.0)
        dst_qlj_params = dst_qlj_params.at[epsilon_indices].multiply(0.5)

        combined_qlj_params = jnp.concatenate([src_qlj_params, dst_qlj_params])
        lambda_plane_idxs = np.zeros(self.mol.GetNumAtoms(), dtype=np.int32)
        lambda_offset_idxs = np.zeros(self.mol.GetNumAtoms(), dtype=np.int32)

        interpolated_potential = nb_potential.interpolate()
        interpolated_potential.set_lambda_plane_idxs(lambda_plane_idxs)
        interpolated_potential.set_lambda_offset_idxs(lambda_offset_idxs)

        return combined_qlj_params, interpolated_potential


class RelativeFreeEnergyForcefield(BaseTopology):
    """
    Used to run the same molecule under different forcefields.
    Currently only changing the nonbonded parameters is supported.

    The parameterize_* methods should be passed a list of parameters
    corresponding to forcefield0 and forcefield1.
    """

    def __init__(self, mol, forcefield0, forcefield1):
        """
        Utility for working with a single ligand.

        Parameter
        ---------
        mol: Chem.Mol
            Ligand to be parameterized

        forcefield0: ff.Forcefield
            A convenience wrapper for forcefield lists.

        forcefield1: ff.Forcefield
            A convenience wrapper for forcefield lists.

        """
        self.mol = mol
        self.ff = forcefield0
        self.ff1 = forcefield1
        self.bt1 = BaseTopology(mol, forcefield1)

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):
        assert self.ff.lj_handle.smirks == self.ff1.lj_handle.smirks, "changing lj smirks is not supported"
        assert self.ff.q_handle.smirks == self.ff1.q_handle.smirks, "changing charge smirks is not supported"
        src_qlj_params, nb_potential = super().parameterize_nonbonded(ff_q_params[0], ff_lj_params[0])
        dst_qlj_params, _ = self.bt1.parameterize_nonbonded(ff_q_params[1], ff_lj_params[1])

        combined_qlj_params = jnp.concatenate([src_qlj_params, dst_qlj_params])
        lambda_plane_idxs = np.zeros(self.mol.GetNumAtoms(), dtype=np.int32)
        lambda_offset_idxs = np.zeros(self.mol.GetNumAtoms(), dtype=np.int32)

        interpolated_potential = nb_potential.interpolate()
        interpolated_potential.set_lambda_plane_idxs(lambda_plane_idxs)
        interpolated_potential.set_lambda_offset_idxs(lambda_offset_idxs)

        return combined_qlj_params, interpolated_potential

    def parameterize_harmonic_bond(self, ff_params):
        assert np.allclose(ff_params[0], ff_params[1]), "changing harmonic bond parameters is not supported"
        assert self.ff.hb_handle.smirks == self.ff1.hb_handle.smirks, "changing harmonic bond smirks is not supported"
        return super().parameterize_harmonic_bond(ff_params[0])

    def parameterize_harmonic_angle(self, ff_params):
        assert np.allclose(ff_params[0], ff_params[1]), "changing harmonic angle parameters is not supported"
        assert self.ff.ha_handle.smirks == self.ff1.ha_handle.smirks, "changing harmonic angle smirks is not supported"
        return super().parameterize_harmonic_angle(ff_params[0])

    def parameterize_periodic_torsion(self, proper_params, improper_params):
        assert np.allclose(proper_params[0], proper_params[1]), "changing proper torsion parameters is not supported"
        assert self.ff.pt_handle.smirks == self.ff1.pt_handle.smirks, "changing proper torsion smirks is not supported"
        assert np.allclose(
            improper_params[0], improper_params[1]
        ), "changing improper torsion parameters is not supported"
        assert (
            self.ff.it_handle.smirks == self.ff1.it_handle.smirks
        ), "changing improper torsion smirks is not supported"
        return super().parameterize_periodic_torsion(proper_params[0], improper_params[0])


class BaseTopologyDecoupling(BaseTopology):
    """
    Decouple a ligand from the environment. The ligand has its charges set to zero
    and lennard jones epsilon halved.

    lambda=0 is the fully interacting state.
    lambda=1 is the non-interacting state.
    """

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):
        qlj_params, nb_potential = super().parameterize_nonbonded(ff_q_params, ff_lj_params)
        charge_indices = jnp.index_exp[:, 0]
        epsilon_indices = jnp.index_exp[:, 2]
        qlj_params = jnp.asarray(qlj_params).at[charge_indices].set(0.0)
        qlj_params = qlj_params.at[epsilon_indices].multiply(0.5)

        return qlj_params, nb_potential


class DualTopology(BaseTopology):
    def __init__(self, mol_a, mol_b, forcefield):
        """
        Utility for working with two ligands via dual topology. Both copies of the ligand
        will be present after merging.

        Parameter
        ---------
        mol_a: ROMol
            First ligand to be parameterized

        mol_b: ROMol
            Second ligand to be parameterized

        forcefield: ff.Forcefield
            A convenience wrapper for forcefield lists.

        """
        self.mol_a = mol_a
        self.mol_b = mol_b
        self.ff = forcefield

    def get_num_atoms(self):
        return self.mol_a.GetNumAtoms() + self.mol_b.GetNumAtoms()

    def get_component_idxs(self) -> List[NDArray]:
        """
        Return the atom indices for the two ligands in
        this topology as a list of NDArray.
        """
        num_a_atoms = self.mol_a.GetNumAtoms()
        num_b_atoms = self.mol_b.GetNumAtoms()
        return [np.arange(num_a_atoms), num_a_atoms + np.arange(num_b_atoms)]

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):

        # dummy is either "a or "b"
        q_params_a = self.ff.q_handle.partial_parameterize(ff_q_params, self.mol_a)
        q_params_b = self.ff.q_handle.partial_parameterize(ff_q_params, self.mol_b)
        lj_params_a = self.ff.lj_handle.partial_parameterize(ff_lj_params, self.mol_a)
        lj_params_b = self.ff.lj_handle.partial_parameterize(ff_lj_params, self.mol_b)

        q_params = jnp.concatenate([q_params_a, q_params_b])
        lj_params = jnp.concatenate([lj_params_a, lj_params_b])

        exclusion_idxs_a, scale_factors_a = nonbonded.generate_exclusion_idxs(
            self.mol_a, scale12=_SCALE_12, scale13=_SCALE_13, scale14=_SCALE_14
        )

        exclusion_idxs_b, scale_factors_b = nonbonded.generate_exclusion_idxs(
            self.mol_b, scale12=_SCALE_12, scale13=_SCALE_13, scale14=_SCALE_14
        )

        mutual_exclusions = []
        mutual_scale_factors = []

        NA = self.mol_a.GetNumAtoms()
        NB = self.mol_b.GetNumAtoms()

        for i in range(NA):
            for j in range(NB):
                mutual_exclusions.append([i, j + NA])
                mutual_scale_factors.append([1.0, 1.0])

        mutual_exclusions = np.array(mutual_exclusions)
        mutual_scale_factors = np.array(mutual_scale_factors)

        combined_exclusion_idxs = np.concatenate([exclusion_idxs_a, exclusion_idxs_b + NA, mutual_exclusions]).astype(
            np.int32
        )

        combined_scale_factors = np.concatenate(
            [
                np.stack([scale_factors_a, scale_factors_a], axis=1),
                np.stack([scale_factors_b, scale_factors_b], axis=1),
                mutual_scale_factors,
            ]
        ).astype(np.float64)

        combined_lambda_plane_idxs = np.zeros(NA + NB, dtype=np.int32)
        combined_lambda_offset_idxs = np.zeros_like(combined_lambda_plane_idxs, dtype=np.int32)

        beta = _BETA
        cutoff = _CUTOFF  # solve for this analytically later

        qlj_params = jnp.concatenate([jnp.reshape(q_params, (-1, 1)), jnp.reshape(lj_params, (-1, 2))], axis=1)

        return qlj_params, potentials.Nonbonded(
            combined_exclusion_idxs,
            combined_scale_factors,
            combined_lambda_plane_idxs,
            combined_lambda_offset_idxs,
            beta,
            cutoff,
        )

    def parameterize_nonbonded_pairlist(self, ff_q_params, ff_lj_params):
        """
        Generate intramolecular nonbonded pairlist, and is mostly identical to the above
        except implemented as a pairlist.
        """
        NA = self.mol_a.GetNumAtoms()

        params_a, pairlist_a = BaseTopology(self.mol_a, self.ff).parameterize_nonbonded_pairlist(
            ff_q_params, ff_lj_params
        )
        params_b, pairlist_b = BaseTopology(self.mol_b, self.ff).parameterize_nonbonded_pairlist(
            ff_q_params, ff_lj_params
        )

        params = np.concatenate([params_a, params_b])

        inclusions_a = pairlist_a.get_idxs()
        inclusions_b = pairlist_b.get_idxs()
        inclusions_b += NA
        inclusion_idxs = np.concatenate([inclusions_a, inclusions_b])

        assert pairlist_a.get_beta() == pairlist_b.get_beta()
        assert pairlist_a.get_cutoff() == pairlist_b.get_cutoff()

        offsets = np.concatenate([pairlist_a.get_offsets(), pairlist_b.get_offsets()])

        return params, potentials.NonbondedPairListPrecomputed(
            inclusion_idxs, offsets, pairlist_a.get_beta(), pairlist_a.get_beta()
        )

    def _parameterize_bonded_term(self, ff_params, bonded_handle, potential):
        offset = self.mol_a.GetNumAtoms()
        params_a, idxs_a = bonded_handle.partial_parameterize(ff_params, self.mol_a)
        params_b, idxs_b = bonded_handle.partial_parameterize(ff_params, self.mol_b)
        params_c = jnp.concatenate([params_a, params_b])
        idxs_c = np.concatenate([idxs_a, idxs_b + offset])
        return params_c, potential(idxs_c)

    def parameterize_harmonic_bond(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.hb_handle, potentials.HarmonicBond)

    def parameterize_harmonic_angle(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.ha_handle, potentials.HarmonicAngle)

    def parameterize_periodic_torsion(self, proper_params, improper_params):
        """
        Parameterize all periodic torsions in the system.
        """
        proper_params, proper_potential = self.parameterize_proper_torsion(proper_params)
        improper_params, improper_potential = self.parameterize_improper_torsion(improper_params)

        combined_params = jnp.concatenate([proper_params, improper_params])
        combined_idxs = np.concatenate([proper_potential.get_idxs(), improper_potential.get_idxs()])

        proper_lambda_mult = proper_potential.get_lambda_mult()
        proper_lambda_offset = proper_potential.get_lambda_offset()

        if proper_lambda_mult is None:
            proper_lambda_mult = np.zeros(len(proper_params))
        if proper_lambda_offset is None:
            proper_lambda_offset = np.ones(len(proper_params))

        improper_lambda_mult = improper_potential.get_lambda_mult()
        improper_lambda_offset = improper_potential.get_lambda_offset()

        if improper_lambda_mult is None:
            improper_lambda_mult = np.zeros(len(improper_params))
        if improper_lambda_offset is None:
            improper_lambda_offset = np.ones(len(improper_params))

        combined_lambda_mult = np.concatenate([proper_lambda_mult, improper_lambda_mult]).astype(np.int32)
        combined_lambda_offset = np.concatenate([proper_lambda_offset, improper_lambda_offset]).astype(np.int32)

        combined_potential = potentials.PeriodicTorsion(combined_idxs, combined_lambda_mult, combined_lambda_offset)
        return combined_params, combined_potential

    def parameterize_proper_torsion(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.pt_handle, potentials.PeriodicTorsion)

    def parameterize_improper_torsion(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.it_handle, potentials.PeriodicTorsion)


class BaseTopologyRHFE(BaseTopology):
    pass


# non-ring torsions are just always turned off at the end-states in the hydration
# free energy test
class DualTopologyRHFE(DualTopology):

    """
    Utility class used for relative hydration free energies. Ligand B is decoupled as lambda goes
    from 0 to 1, while ligand A is fully coupled. At the same time, at lambda=0, ligand B and ligand A
    have their charges and epsilons reduced by half.
    """

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):

        qlj_params, nb_potential = super().parameterize_nonbonded(ff_q_params, ff_lj_params)

        # halve the strength of the charge and the epsilon parameters
        charge_indices = jnp.index_exp[:, 0]
        epsilon_indices = jnp.index_exp[:, 2]

        src_qlj_params = jnp.asarray(qlj_params).at[charge_indices].multiply(0.5)
        src_qlj_params = jnp.asarray(src_qlj_params).at[epsilon_indices].multiply(0.5)

        dst_qlj_params = qlj_params
        combined_qlj_params = jnp.concatenate([src_qlj_params, dst_qlj_params])

        combined_lambda_plane_idxs = np.zeros(self.mol_a.GetNumAtoms() + self.mol_b.GetNumAtoms(), dtype=np.int32)
        combined_lambda_offset_idxs = np.concatenate(
            [np.zeros(self.mol_a.GetNumAtoms(), dtype=np.int32), np.ones(self.mol_b.GetNumAtoms(), dtype=np.int32)]
        )

        nb_potential.set_lambda_plane_idxs(combined_lambda_plane_idxs)
        nb_potential.set_lambda_offset_idxs(combined_lambda_offset_idxs)

        return combined_qlj_params, nb_potential.interpolate()


class DualTopologyMinimization(DualTopology):
    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):

        # both mol_a and mol_b are standardized.
        # we don't actually need derivatives for this stage.
        qlj_params, nb_potential = super().parameterize_nonbonded(ff_q_params, ff_lj_params)

        N_A, N_B = self.mol_a.GetNumAtoms(), self.mol_b.GetNumAtoms()
        combined_lambda_plane_idxs = np.zeros(N_A + N_B, dtype=np.int32)
        combined_lambda_offset_idxs = np.ones(N_A + N_B, dtype=np.int32)

        nb_potential.set_lambda_offset_idxs(combined_lambda_offset_idxs)
        nb_potential.set_lambda_plane_idxs(combined_lambda_plane_idxs)

        return qlj_params, nb_potential


class SingleTopology:
    def __init__(self, mol_a, mol_b, core, ff, minimize: bool = False):
        """
        SingleTopology combines two molecules through a common core. The combined mol has
        atom indices laid out such that mol_a is identically mapped to the combined mol indices.
        The atoms in the mol_b's R-group is then glued on to resulting molecule.

        Parameters
        ----------
        mol_a: ROMol
            First ligand

        mol_b: ROMol
            Second ligand

        core: np.array (C, 2)
            Atom mapping from mol_a to to mol_b

        ff: ff.Forcefield
            Forcefield to be used for parameterization.

        minimize: bool
            Whether both R groups should be interacting at lambda=0.5

        """
        self.mol_a = mol_a
        self.mol_b = mol_b
        self.ff = ff
        self.core = core
        self.minimize = minimize

        assert core.shape[1] == 2

        # map into idxs in the combined molecule
        self.a_to_c = np.arange(mol_a.GetNumAtoms(), dtype=np.int32)  # identity
        self.b_to_c = np.zeros(mol_b.GetNumAtoms(), dtype=np.int32) - 1

        self.NC = mol_a.GetNumAtoms() + mol_b.GetNumAtoms() - len(core)

        # mark membership:
        # 0: Core
        # 1: R_A (default)
        # 2: R_B
        self.c_flags = np.ones(self.get_num_atoms(), dtype=np.int32)

        for a, b in core:
            self.c_flags[a] = 0
            self.b_to_c[b] = a

        iota = self.mol_a.GetNumAtoms()
        for b_idx, c_idx in enumerate(self.b_to_c):
            if c_idx == -1:
                self.b_to_c[b_idx] = iota
                self.c_flags[iota] = 2
                iota += 1

        # test for uniqueness in core idxs for each mol
        assert len(set(tuple(core[:, 0]))) == len(core[:, 0])
        assert len(set(tuple(core[:, 1]))) == len(core[:, 1])

        self.assert_factorizability()

    def get_component_idxs(self) -> List[NDArray]:
        """
        Return the atom indices for the two ligands in
        this topology as a list of NDArray. Both lists
        will contain atom indices for the core atoms
        as well as the unique atom indices for each ligand.
        """
        return [self.a_to_c, self.b_to_c]

    def _identify_offending_core_indices(self):
        """Identifies atoms involved in violations of a factorizability assumption,
        but doesn't immediately raise an error.
        Later, could use this list to:
        * plot / debug
        * if in a "repair_mode", attempt to repair the mapping by removing offending atoms
        * otherwise, raise atom mapping error if any atoms were identified
        """

        # Test that R-groups can be properly factorized out in the proposed
        # mapping. The requirement is that R-groups must be branched from exactly
        # a single atom on the core.

        offending_core_indices = []

        # first convert to a dense graph
        N = self.get_num_atoms()
        dense_graph = np.zeros((N, N), dtype=np.int32)

        for bond in self.mol_a.GetBonds():
            i, j = self.a_to_c[bond.GetBeginAtomIdx()], self.a_to_c[bond.GetEndAtomIdx()]
            dense_graph[i, j] = 1
            dense_graph[j, i] = 1

        for bond in self.mol_b.GetBonds():
            i, j = self.b_to_c[bond.GetBeginAtomIdx()], self.b_to_c[bond.GetEndAtomIdx()]
            dense_graph[i, j] = 1
            dense_graph[j, i] = 1

            # sparsify to simplify and speed up traversal code
        sparse_graph = []
        for row in dense_graph:
            nbs = []
            for col_idx, col in enumerate(row):
                if col == 1:
                    nbs.append(col_idx)
            sparse_graph.append(nbs)

        def visit(i, visited):
            if i in visited:
                return
            else:
                visited.add(i)
                if self.c_flags[i] != 0:
                    for nb in sparse_graph[i]:
                        visit(nb, visited)
                else:
                    return

        for c_idx, group in enumerate(self.c_flags):
            # 0 core, 1 R_A, 2: R_B
            if group != 0:
                seen = set()
                visit(c_idx, seen)
                # (ytz): exactly one of seen should belong to core
                if np.sum(np.array([self.c_flags[x] for x in seen]) == 0) != 1:
                    offending_core_indices.append(c_idx)

        return offending_core_indices

    def assert_factorizability(self):
        """
        Number of atoms in the combined mol

        TODO: add a reference to Boresch paper describing the assumption being checked
        """
        offending_core_indices = self._identify_offending_core_indices()
        num_problems = len(offending_core_indices)
        if num_problems > 0:

            # TODO: revisit how to get atom pair indices -- this goes out of bounds
            # bad_pairs = [tuple(self.core[c_index]) for c_index in offending_core_indices]

            message = f"""Atom Mapping Error: the resulting map is non-factorizable!
            (The map contained  {num_problems} violations of the factorizability assumption.)
            """
            raise AtomMappingError(message)

    def get_num_atoms(self):
        return self.NC

    def interpolate_params(self, params_a, params_b):
        """
        Interpolate two sets of per-particle parameters.

        This can be used to interpolate masses, coordinates, etc.

        Parameters
        ----------
        params_a: np.ndarray, shape [N_A, ...]
            Parameters for the mol_a

        params_b: np.ndarray, shape [N_B, ...]
            Parameters for the mol_b

        Returns
        -------
        tuple: (src, dst)
            Two np.ndarrays each of shape [N_C, ...]

        """

        src_params = [None] * self.get_num_atoms()
        dst_params = [None] * self.get_num_atoms()

        for a_idx, c_idx in enumerate(self.a_to_c):
            src_params[c_idx] = params_a[a_idx]
            dst_params[c_idx] = params_a[a_idx]

        for b_idx, c_idx in enumerate(self.b_to_c):
            dst_params[c_idx] = params_b[b_idx]
            if src_params[c_idx] is None:
                src_params[c_idx] = params_b[b_idx]

        return jnp.array(src_params), jnp.array(dst_params)

    def interpolate_nonbonded_params(self, params_a, params_b):
        """
        Special interpolation method for nonbonded parameters. For R-group atoms,
        their charges and vdw eps parameters are scaled to zero. Vdw sigma
        remains unchanged. This method is needed in order to ensure that R-groups
        that branch from multiple distinct attachment points are fully non-interacting
        to allow for factorization of the partition function. In order words, this function
        implements essentially the non-softcore part of parameter interpolation.

        Parameters
        ----------
        params_a: np.ndarray, shape [N_A, 3]
            Nonbonded parameters for the mol_a

        params_b: np.ndarray, shape [N_B, 3]
            Nonbonded parameters for the mol_b

        Returns
        -------
        tuple: (src, dst)
            Two np.ndarrays each of shape [N_C, ...]

        """

        src_params = [None] * self.get_num_atoms()
        dst_params = [None] * self.get_num_atoms()

        # src -> dst is turning off the parameter
        for a_idx, c_idx in enumerate(self.a_to_c):
            params = params_a[a_idx]
            src_params[c_idx] = params
            if self.c_flags[c_idx] != 0:
                assert self.c_flags[c_idx] == 1
                dst_params[c_idx] = jnp.array([0, params[1], 0])  # q, sig, eps

        # b is initially decoupled
        for b_idx, c_idx in enumerate(self.b_to_c):
            params = params_b[b_idx]
            dst_params[c_idx] = params
            # this will already be processed when looping over a
            if self.c_flags[c_idx] == 0:
                assert src_params[c_idx] is not None
            else:
                assert self.c_flags[c_idx] == 2
                src_params[c_idx] = jnp.array([0, params[1], 0])  # q, sig, eps

        return jnp.array(src_params), jnp.array(dst_params)

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):
        # Nonbonded potentials combine through parameter interpolation, not energy interpolation.
        # They may or may not operate through 4D decoupling depending on the atom mapping. If an atom is
        # unique, it is kept at full strength and not switched off.

        q_params_a = self.ff.q_handle.partial_parameterize(ff_q_params, self.mol_a)
        q_params_b = self.ff.q_handle.partial_parameterize(ff_q_params, self.mol_b)  # HARD TYPO
        lj_params_a = self.ff.lj_handle.partial_parameterize(ff_lj_params, self.mol_a)
        lj_params_b = self.ff.lj_handle.partial_parameterize(ff_lj_params, self.mol_b)

        qlj_params_a = jnp.concatenate([jnp.reshape(q_params_a, (-1, 1)), jnp.reshape(lj_params_a, (-1, 2))], axis=1)
        qlj_params_b = jnp.concatenate([jnp.reshape(q_params_b, (-1, 1)), jnp.reshape(lj_params_b, (-1, 2))], axis=1)

        qlj_params_src, qlj_params_dst = self.interpolate_nonbonded_params(qlj_params_a, qlj_params_b)
        qlj_params = jnp.concatenate([qlj_params_src, qlj_params_dst])

        exclusion_idxs_a, scale_factors_a = nonbonded.generate_exclusion_idxs(
            self.mol_a, scale12=_SCALE_12, scale13=_SCALE_13, scale14=_SCALE_14
        )

        exclusion_idxs_b, scale_factors_b = nonbonded.generate_exclusion_idxs(
            self.mol_b, scale12=_SCALE_12, scale13=_SCALE_13, scale14=_SCALE_14
        )

        # (ytz): use the same scale factors of LJ & charges for now
        # this isn't quite correct as the LJ/Coluomb may be different in
        # different forcefields.
        scale_factors_a = np.stack([scale_factors_a, scale_factors_a], axis=1)
        scale_factors_b = np.stack([scale_factors_b, scale_factors_b], axis=1)

        combined_exclusion_dict = dict()

        for ij, scale in zip(exclusion_idxs_a, scale_factors_a):
            ij = tuple(sorted(self.a_to_c[ij]))
            if ij in combined_exclusion_dict:
                np.testing.assert_array_equal(combined_exclusion_dict[ij], scale)
            else:
                combined_exclusion_dict[ij] = scale

        for ij, scale in zip(exclusion_idxs_b, scale_factors_b):
            ij = tuple(sorted(self.b_to_c[ij]))
            if ij in combined_exclusion_dict:
                np.testing.assert_array_equal(combined_exclusion_dict[ij], scale)
            else:
                combined_exclusion_dict[ij] = scale

        combined_exclusion_idxs = []
        combined_scale_factors = []

        for e, s in combined_exclusion_dict.items():
            combined_exclusion_idxs.append(e)
            combined_scale_factors.append(s)

        combined_exclusion_idxs = np.array(combined_exclusion_idxs)
        combined_scale_factors = np.array(combined_scale_factors)

        # (ytz): we don't need exclusions between R_A and R_B will never see each other
        # under this decoupling scheme. They will always be at cutoff apart from each other.

        # plane_idxs: RA = Core = 0, RB = -1
        # offset_idxs: Core = 0, RA = RB = +1
        combined_lambda_plane_idxs = np.zeros(self.get_num_atoms(), dtype=np.int32)
        combined_lambda_offset_idxs = np.zeros(self.get_num_atoms(), dtype=np.int32)

        # w = cutoff * (lambda_plane_idxs + lambda_offset_idxs * lamb)
        for atom, group in enumerate(self.c_flags):
            if group == 0:
                # core atom
                combined_lambda_plane_idxs[atom] = 0
                combined_lambda_offset_idxs[atom] = 0
            elif group == 1:  # RA or RA and RB (if minimize) interact at lamb=0.0
                combined_lambda_plane_idxs[atom] = 0
                combined_lambda_offset_idxs[atom] = 1
            elif group == 2:  # R Groups of Mol B
                combined_lambda_plane_idxs[atom] = -1
                combined_lambda_offset_idxs[atom] = 1
            else:
                assert 0, f"Unknown group {group}"

        beta = _BETA
        cutoff = _CUTOFF  # solve for this analytically later

        nb = potentials.NonbondedInterpolated(
            combined_exclusion_idxs,
            combined_scale_factors,
            combined_lambda_plane_idxs,
            combined_lambda_offset_idxs,
            beta,
            cutoff,
        )

        return qlj_params, nb

    @staticmethod
    def _concatenate(arrs):
        non_empty = []
        for arr in arrs:
            if len(arr) != 0:
                non_empty.append(jnp.array(arr))
        return jnp.concatenate(non_empty)

    def _parameterize_bonded_term(self, ff_params, bonded_handle, potential):
        # Bonded terms are defined as follows:
        # If a bonded term is comprised exclusively of atoms in the core region, then
        # its energy its interpolated from the on-state to off-state.
        # Otherwise (i.e. it has one atom that is not in the core region), the bond term
        # is defined as unique, and is on at all times.
        # This means that the end state will contain dummy atoms that is not the true end-state,
        # but contains an analytical correction (through Boresch) that can be cancelled out.

        params_a, idxs_a = bonded_handle.partial_parameterize(ff_params, self.mol_a)
        params_b, idxs_b = bonded_handle.partial_parameterize(ff_params, self.mol_b)

        core_params_a = []
        core_params_b = []
        unique_params_r = []

        core_idxs_a = []
        core_idxs_b = []
        unique_idxs_r = []
        for p, old_atoms in zip(params_a, idxs_a):
            new_atoms = self.a_to_c[old_atoms]
            if np.all(self.c_flags[new_atoms] == 0):
                core_params_a.append(p)
                core_idxs_a.append(new_atoms)
            else:
                unique_params_r.append(p)
                unique_idxs_r.append(new_atoms)

        for p, old_atoms in zip(params_b, idxs_b):
            new_atoms = self.b_to_c[old_atoms]
            if np.all(self.c_flags[new_atoms] == 0):
                core_params_b.append(p)
                core_idxs_b.append(new_atoms)
            else:
                unique_params_r.append(p)
                unique_idxs_r.append(new_atoms)

        core_params_a = jnp.array(core_params_a)
        core_params_b = jnp.array(core_params_b)
        unique_params_r = jnp.array(unique_params_r)

        # number of parameters per term (2 for bonds, 2 for angles, 3 for torsions)
        # P = params_a.shape[-1]  # TODO: note P unused

        combined_params = self._concatenate([core_params_a, core_params_b, unique_params_r])

        # number of atoms involved in the bonded term
        K = idxs_a.shape[-1]

        core_idxs_a = np.array(core_idxs_a, dtype=np.int32).reshape((-1, K))
        core_idxs_b = np.array(core_idxs_b, dtype=np.int32).reshape((-1, K))
        unique_idxs_r = np.array(unique_idxs_r, dtype=np.int32).reshape((-1, K))  # always on

        # TODO: assert `len(core_idxs_a) == len(core_idxs_b)` in a more fine-grained way

        combined_idxs = np.concatenate([core_idxs_a, core_idxs_b, unique_idxs_r])

        lamb_mult = np.array(
            [-1] * len(core_idxs_a) + [1] * len(core_idxs_b) + [0] * len(unique_idxs_r), dtype=np.int32
        )
        lamb_offset = np.array(
            [1] * len(core_idxs_a) + [0] * len(core_idxs_b) + [1] * len(unique_idxs_r), dtype=np.int32
        )

        u_fn = potential(combined_idxs, lamb_mult, lamb_offset)
        return combined_params, u_fn

    def parameterize_harmonic_bond(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.hb_handle, potentials.HarmonicBond)

    def parameterize_harmonic_angle(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.ha_handle, potentials.HarmonicAngle)

    def parameterize_proper_torsion(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.pt_handle, potentials.PeriodicTorsion)

    def parameterize_improper_torsion(self, ff_params):
        return self._parameterize_bonded_term(ff_params, self.ff.it_handle, potentials.PeriodicTorsion)

    def parameterize_periodic_torsion(self, proper_params, improper_params):
        """
        Parameterize all periodic torsions in the system.
        """
        proper_params, proper_potential = self.parameterize_proper_torsion(proper_params)
        improper_params, improper_potential = self.parameterize_improper_torsion(improper_params)
        combined_params = jnp.concatenate([proper_params, improper_params])
        combined_idxs = np.concatenate([proper_potential.get_idxs(), improper_potential.get_idxs()])
        combined_lambda_mult = np.concatenate(
            [proper_potential.get_lambda_mult(), improper_potential.get_lambda_mult()]
        )
        combined_lambda_offset = np.concatenate(
            [proper_potential.get_lambda_offset(), improper_potential.get_lambda_offset()]
        )
        combined_potential = potentials.PeriodicTorsion(combined_idxs, combined_lambda_mult, combined_lambda_offset)
        return combined_params, combined_potential


class DualTopologyChargeConversion(DualTopology):
    """
    Let A and B be the two ligands of interest (typically both occupying the binding pocket)
    Assume that exclusions are already defined between atoms in A and atoms in B.

    Let this topology class compute the free energy associated with transferring the charge
    from molecule A onto molecule B.

    lambda=0: ligand A (charged), ligand B (decharged)
    lambda=1: ligand A (decharged), ligand B (charged)

    """

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):
        qlj_params_ab, nb_potential = super().parameterize_nonbonded(ff_q_params, ff_lj_params)
        num_a_atoms = self.mol_a.GetNumAtoms()
        charge_indices_b = jnp.index_exp[num_a_atoms:, 0]
        epsilon_indices_b = jnp.index_exp[num_a_atoms:, 2]

        charge_indices_a = jnp.index_exp[:num_a_atoms, 0]
        epsilon_indices_a = jnp.index_exp[:num_a_atoms, 2]

        qlj_params_src = qlj_params_ab.at[charge_indices_b].set(0.0)
        qlj_params_src = qlj_params_src.at[epsilon_indices_b].multiply(0.5)
        qlj_params_dst = qlj_params_ab.at[charge_indices_a].set(0.0)
        qlj_params_dst = qlj_params_dst.at[epsilon_indices_a].multiply(0.5)
        combined_qlj_params = jnp.concatenate([qlj_params_src, qlj_params_dst])
        interpolated_potential = nb_potential.interpolate()

        total_atoms = num_a_atoms + self.mol_b.GetNumAtoms()

        # probably already set to zeros by default
        combined_lambda_plane_idxs = np.zeros(total_atoms, dtype=np.int32)
        combined_lambda_offset_idxs = np.zeros(total_atoms, dtype=np.int32)
        interpolated_potential.set_lambda_plane_idxs(combined_lambda_plane_idxs)
        interpolated_potential.set_lambda_offset_idxs(combined_lambda_offset_idxs)

        return combined_qlj_params, interpolated_potential


class DualTopologyDecoupling(DualTopology):
    """
    Given two ligands A and B, let the end-states be:

    lambda=0 A is fully charged, and interacting with the environment.
             B is decharged, but "inserted"/interacting with the environment.

    lambda=1 A is fully charged, and interacting with the environment.
             B is decharged, and non-interacting with the environment.

    """

    def parameterize_nonbonded(self, ff_q_params, ff_lj_params):
        qlj_params_combined, nb_potential = super().parameterize_nonbonded(ff_q_params, ff_lj_params)
        num_a_atoms = self.mol_a.GetNumAtoms()
        charge_indices_b = jnp.index_exp[num_a_atoms:, 0]
        epsilon_indices_b = jnp.index_exp[num_a_atoms:, 2]

        qlj_params_combined = jnp.asarray(qlj_params_combined).at[charge_indices_b].set(0.0)
        qlj_params_combined = qlj_params_combined.at[epsilon_indices_b].multiply(0.5)

        num_b_atoms = self.mol_b.GetNumAtoms()
        combined_lambda_plane_idxs = np.zeros(num_a_atoms + num_b_atoms, dtype=np.int32)
        combined_lambda_offset_idxs = np.concatenate(
            [np.zeros(num_a_atoms, dtype=np.int32), np.ones(num_b_atoms, dtype=np.int32)]
        )
        nb_potential.set_lambda_plane_idxs(combined_lambda_plane_idxs)
        nb_potential.set_lambda_offset_idxs(combined_lambda_offset_idxs)

        return qlj_params_combined, nb_potential
