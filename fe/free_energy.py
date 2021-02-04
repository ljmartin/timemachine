from jax.config import config; config.update("jax_enable_x64", True)

import jax
import numpy as np

from fe import topology

from timemachine.lib import potentials, custom_ops
from timemachine.lib import LangevinIntegrator

from ff.handlers import openmm_deserializer

def get_romol_conf(mol):
    """Coordinates of mol's 0th conformer, in nanometers"""
    conformer = mol.GetConformer(0)
    guest_conf = np.array(conformer.GetPositions(), dtype=np.float64)
    return guest_conf/10 # from angstroms to nm

class BaseFreeEnergy():

    @staticmethod
    def _get_integrator(combined_masses):
        """
        Get a integrator. The resulting impl must be bound to a python handle
        whose lifetime is concurrent with that of the context.
        """
        seed = np.random.randint(np.iinfo(np.int32).max)

        return LangevinIntegrator(
            300.0,
            1.5e-3,
            1.0,
            combined_masses,
            seed
        )

    @staticmethod
    def _get_system_params_and_potentials(ff_params, topology):

        ff_tuples = [
            [topology.parameterize_harmonic_bond, (ff_params[0],)],
            [topology.parameterize_harmonic_angle, (ff_params[1],)],
            [topology.parameterize_periodic_torsion, (ff_params[2], ff_params[3])],
            [topology.parameterize_nonbonded, (ff_params[4], ff_params[5])]
        ]

        final_params = []
        final_potentials = []

        for fn, params in ff_tuples:
            combined_params, combined_potential = fn(*params)
            final_potentials.append(combined_potential)
            final_params.append(combined_params)

        return final_params, final_potentials

# this class is serializable.
class AbsoluteFreeEnergy(BaseFreeEnergy):

    def __init__(self, mol, ff):
        """
        Compute the absolute free energy of a molecule via 4D decoupling.

        Parameters
        ----------
        mol: rdkit mol
            Ligand to be decoupled

        ff: ff.Forcefield
            Ligand forcefield

        """
        self.mol = mol
        self.ff = ff
        self.top = topology.BaseTopology(mol, ff)


    def prepare_host_edge(self, ff_params, host_system, host_coords):
        """
        Prepares the host-edge system

        Parameters
        ----------
        ff_params: tuple of np.array
            Exactly equal to bond_params, angle_params, proper_params, improper_params, charge_params, lj_params

        host_system: openmm.System
            openmm System object to be deserialized

        host_coords: np.array
            Nx3 array of atomic coordinates

        Returns
        -------
        4 tuple
            unbound_potentials, system_params, combined_masses, combined_coords

        """
        ligand_masses = [a.GetMass() for a in self.mol.GetAtoms()]
        ligand_coords = get_romol_conf(self.mol)

        host_bps, host_masses = openmm_deserializer.deserialize_system(host_system, cutoff=1.2)
        num_host_atoms = host_coords.shape[0]

        hgt = topology.HostGuestTopology(host_bps, self.top)

        final_params, final_potentials = self._get_system_params_and_potentials(ff_params, hgt)

        combined_masses = np.concatenate([host_masses, ligand_masses])
        combined_coords = np.concatenate([host_coords, ligand_coords])

        return final_potentials, final_params, combined_masses, combined_coords


# this class is serializable.
class RelativeFreeEnergy(BaseFreeEnergy):

    def __init__(self, mol_a, mol_b, core, ff):
        self.mol_a = mol_a
        self.mol_b = mol_b
        self.core = core
        self.top = topology.SingleTopology(mol_a, mol_b, core, ff)

    def _get_integrator(self, combined_masses):
        """
        Get a integrator. The resulting impl must be bound to a python handle
        whose lifetime is concurrent with that of the context.
        """
        seed = np.random.randint(np.iinfo(np.int32).max)

        return LangevinIntegrator(
            300.0,
            1.5e-3,
            1.0,
            combined_masses,
            seed
        )

    def prepare_vacuum_edge(self, ff_params):
        """
        Prepares the vacuum system.

        Parameters
        ----------
        ff_params: tuple of np.array
            Exactly equal to bond_params, angle_params, proper_params, improper_params, charge_params, lj_params

        Returns
        -------
        4 tuple
            unbound_potentials, system_parameters, combined_masses, combined_coords

        """
        ligand_masses_a = [a.GetMass() for a in self.mol_a.GetAtoms()]
        ligand_masses_b = [b.GetMass() for b in self.mol_b.GetAtoms()]

        ligand_coords_a = get_romol_conf(self.mol_a)
        ligand_coords_b = get_romol_conf(self.mol_b)

        final_params, final_potentials = self._get_system_params_and_potentials(ff_params, self.top)

        combined_masses = np.mean(self.top.interpolate_params(ligand_masses_a, ligand_masses_b), axis=0)
        combined_coords = np.mean(self.top.interpolate_params(ligand_coords_a, ligand_coords_b), axis=0)

        return final_potentials, final_params, combined_masses, combined_coords

    def prepare_host_edge(self, ff_params, host_system, host_coords):
        """
        Prepares the host-edge system

        Parameters
        ----------
        ff_params: tuple of np.array
            Exactly equal to bond_params, angle_params, proper_params, improper_params, charge_params, lj_params

        host_system: openmm.System
            openmm System object to be deserialized

        host_coords: np.array
            Nx3 array of atomic coordinates

        Returns
        -------
        4 tuple
            unbound_potentials, system_params, combined_masses, combined_coords

        """

        ligand_masses_a = [a.GetMass() for a in self.mol_a.GetAtoms()]
        ligand_masses_b = [b.GetMass() for b in self.mol_b.GetAtoms()]

        # extract the 0th conformer
        ligand_coords_a = get_romol_conf(self.mol_a)
        ligand_coords_b = get_romol_conf(self.mol_b)

        host_bps, host_masses = openmm_deserializer.deserialize_system(host_system, cutoff=1.2)
        num_host_atoms = host_coords.shape[0]

        hgt = topology.HostGuestTopology(host_bps, self.top)

        final_params, final_potentials = self._get_system_params_and_potentials(ff_params, hgt)

        combined_masses = np.concatenate([host_masses, np.mean(self.top.interpolate_params(ligand_masses_a, ligand_masses_b), axis=0)])
        combined_coords = np.concatenate([host_coords, np.mean(self.top.interpolate_params(ligand_coords_a, ligand_coords_b), axis=0)])

        return final_potentials, final_params, combined_masses, combined_coords
