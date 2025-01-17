import numpy as np
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from timemachine.fe import chiral_utils, utils
from timemachine.fe.chiral_utils import ChiralCheckMode, ChiralRestrIdxSet, find_atom_map_chiral_conflicts
from timemachine.potentials.chiral_restraints import U_chiral_atom_batch, U_chiral_bond_batch

pytestmark = [pytest.mark.nocuda]


def test_setup_chiral_atom_restraints():
    """On a methane conformer, assert that permuting coordinates or permuting restr_idxs
    both independently toggle the chiral restraint"""
    mol = Chem.MolFromMolBlock(
        """
  Mrv2202 06072215563D

  5  4  0  0  0  0            999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -0.3633   -0.5138    0.8900 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.0900    0.0000    0.0000 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.3633    1.0277    0.0000 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.3633   -0.5138   -0.8900 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  1  3  1  0  0  0  0
  1  4  1  0  0  0  0
  1  5  1  0  0  0  0
M  END
$$$$""",
        removeHs=False,
    )

    # needs to be batched in order for jax to play nicely
    x0 = utils.get_romol_conf(mol)
    normal_restr_idxs = np.array(chiral_utils.setup_chiral_atom_restraints(mol, x0, 0))

    x0_inverted = x0[[0, 2, 1, 3, 4]]  # swap two atoms
    inverted_restr_idxs = np.array(chiral_utils.setup_chiral_atom_restraints(mol, x0_inverted, 0))

    # check the sign of the resulting idxs
    k = 1000.0
    assert np.all(np.asarray(U_chiral_atom_batch(x0, normal_restr_idxs, k)) == 0)
    assert np.all(np.asarray(U_chiral_atom_batch(x0, inverted_restr_idxs, k)) > 0)
    assert np.all(np.asarray(U_chiral_atom_batch(x0_inverted, normal_restr_idxs, k)) > 0)
    assert np.all(np.asarray(U_chiral_atom_batch(x0_inverted, inverted_restr_idxs, k)) == 0)


def test_setup_chiral_bond_restraints():
    """On a 'Cl/C(F)=N/F' conformer, assert that flipping a dihedral angle or permuting restr_idxs
    both independently toggle the chiral bond restraint"""

    mol_cis = Chem.MolFromSmiles(r"Cl\C(F)=N/F")
    mol_trans = Chem.MolFromSmiles(r"Cl\C(F)=N\F")

    AllChem.EmbedMolecule(mol_cis)
    AllChem.EmbedMolecule(mol_trans)

    # needs to be batched in order for jax to play nicely
    x0_cis = utils.get_romol_conf(mol_cis)
    x0_trans = utils.get_romol_conf(mol_trans)
    src_atom = 1
    dst_atom = 3
    normal_restr_idxs, signs = chiral_utils.setup_chiral_bond_restraints(mol_cis, x0_cis, src_atom, dst_atom)
    normal_restr_idxs = np.array(normal_restr_idxs)
    signs = np.array(signs)
    inverted_restr_idxs, inverted_signs = chiral_utils.setup_chiral_bond_restraints(
        mol_trans, x0_trans, src_atom, dst_atom
    )
    inverted_restr_idxs = np.array(inverted_restr_idxs)
    inverted_signs = np.array(inverted_signs)
    k = 1000.0

    assert np.all(np.asarray(U_chiral_bond_batch(x0_cis, normal_restr_idxs, k, signs)) == 0)
    assert np.all(np.asarray(U_chiral_bond_batch(x0_cis, inverted_restr_idxs, k, inverted_signs)) > 0)
    assert np.all(np.asarray(U_chiral_bond_batch(x0_trans, normal_restr_idxs, k, signs)) > 0)
    assert np.all(np.asarray(U_chiral_bond_batch(x0_trans, inverted_restr_idxs, k, inverted_signs)) == 0)


def test_find_chiral_atoms():
    # test that we can identify chiral atoms ub a couple of tetrahedral and pyramidal
    # molecules.

    mol = Chem.AddHs(Chem.MolFromSmiles(r"FC(Cl)Br"))
    res = chiral_utils.find_chiral_atoms(mol)
    assert res == set([1])

    mol = Chem.AddHs(Chem.MolFromSmiles(r"FN(Cl)Br"))
    res = chiral_utils.find_chiral_atoms(mol)
    assert res == set([])

    mol = Chem.AddHs(Chem.MolFromSmiles(r"FS(Cl)Br"))
    res = chiral_utils.find_chiral_atoms(mol)
    assert res == set([1])

    mol = Chem.AddHs(Chem.MolFromSmiles(r"FP(Cl)Br"))
    res = chiral_utils.find_chiral_atoms(mol)
    assert res == set([1])

    mol = Chem.AddHs(Chem.MolFromSmiles(r"C1CC2CCC3CCC1N23"))
    res = chiral_utils.find_chiral_atoms(mol)
    assert res == set([0, 1, 2, 3, 4, 5, 6, 7, 8])  # TODO: add atom idx 9 if we handle pyramidal nitrogens

    mol = Chem.AddHs(Chem.MolFromSmiles(r"n1ccccc1"))
    res = chiral_utils.find_chiral_atoms(mol)
    assert res == set()


def test_find_chiral_bonds():
    # test that we can identify chiral bonds in a couple of simple systems
    # involving double bonds and amides

    mol = Chem.MolFromSmiles(r"FOOF")
    res = chiral_utils.find_chiral_bonds(mol)
    assert res == set([])

    mol = Chem.MolFromSmiles(r"FN=NF")
    res = chiral_utils.find_chiral_bonds(mol)
    assert res == set([(1, 2)])

    mol = Chem.AddHs(Chem.MolFromSmiles(r"c1cocc1"))
    res = chiral_utils.find_chiral_bonds(mol)
    assert res == set()

    mol = Chem.AddHs(Chem.MolFromSmiles(r"C1=CCC=CO1"))
    res = chiral_utils.find_chiral_bonds(mol)
    assert res == set([(0, 1), (3, 4)])

    mol = Chem.AddHs(Chem.MolFromSmiles(r"FNC=O"))
    res = chiral_utils.find_chiral_bonds(mol)
    assert res == set([(1, 2)])

    # tautomer of the above
    mol = Chem.AddHs(Chem.MolFromSmiles(r"O\C=N\F"))
    res = chiral_utils.find_chiral_bonds(mol)
    assert res == set([(1, 2)])

    mol = Chem.AddHs(Chem.MolFromSmiles(r"N(C)C(C)=O"))
    res = chiral_utils.find_chiral_bonds(mol)
    assert res == set([(0, 2)])


# next few strings named molblock_{smi} are generated
# using rdkit version 2022.03.5:
# -----------------------------------------
# mol = Chem.AddHs(Chem.MolFromSmiles(smi))
# AllChem.EmbedMolecule(mol, randomSeed=0)
# print(Chem.MolToMolBlock(mol))
# -----------------------------------------
molblock_C = """
     RDKit          3D

  5  4  0  0  0  0  0  0  0  0999 V2000
    0.0051   -0.0106    0.0060 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.5497    0.7554   -0.5970 H   0  0  0  0  0  0  0  0  0  0  0  0
    0.7498   -0.5879    0.5853 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.5868   -0.6521   -0.6761 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.7178    0.4953    0.6818 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  1  3  1  0
  1  4  1  0
  1  5  1  0
M  END"""

molblock_N = """
     RDKit          3D

  4  3  0  0  0  0  0  0  0  0999 V2000
    0.0195   -0.0020    0.2429 N   0  0  0  0  0  0  0  0  0  0  0  0
    0.9942   -0.1240   -0.0852 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.5944   -0.7730   -0.0788 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.4193    0.8989   -0.0789 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  1  3  1  0
  1  4  1  0
M  END"""

molblock_CC = """
     RDKit          3D

  8  7  0  0  0  0  0  0  0  0999 V2000
   -0.7455    0.0414    0.0117 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.7473    0.0029    0.0012 C   0  0  0  0  0  0  0  0  0  0  0  0
   -1.1297   -0.6374    0.8144 H   0  0  0  0  0  0  0  0  0  0  0  0
   -1.1849    1.0256    0.1996 H   0  0  0  0  0  0  0  0  0  0  0  0
   -1.1999   -0.3346   -0.9389 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.0842   -0.7365   -0.7732 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.2266    0.9617   -0.2681 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.2019   -0.3231    0.9532 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  1  3  1  0
  1  4  1  0
  1  5  1  0
  2  6  1  0
  2  7  1  0
  2  8  1  0
M  END"""

molblock_CN = """
     RDKit          3D

  7  6  0  0  0  0  0  0  0  0999 V2000
   -0.5732    0.0243   -0.0031 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.8233   -0.0403   -0.0341 N   0  0  0  0  0  0  0  0  0  0  0  0
   -1.0026   -0.4265    0.9182 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.9088    1.0746   -0.0741 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.9930   -0.5345   -0.8755 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.2903   -0.9052    0.2005 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.3640    0.8076   -0.1320 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  1  3  1  0
  1  4  1  0
  1  5  1  0
  2  6  1  0
  2  7  1  0
M  END"""


def test_chiral_conflict_flip():
    # exercise case of no conflicts or a flip conflict

    mol_a = Chem.MolFromMolBlock(molblock_C, removeHs=False)
    mol_b = Chem.MolFromMolBlock(molblock_C, removeHs=False)

    conf_a = mol_a.GetConformer(0).GetPositions()
    conf_b = mol_b.GetConformer(0).GetPositions()

    chiral_set_a = ChiralRestrIdxSet.from_mol(mol_a, conf_a)
    chiral_set_b = ChiralRestrIdxSet.from_mol(mol_b, conf_b)

    assert len(chiral_set_a.restr_idxs) == 4
    assert len(chiral_set_b.restr_idxs) == 4

    identity_map = np.array([(i, i) for i in range(len(conf_a))])

    # swap any pair of atoms around a tetrahedral center to flip chirality
    swap_map = np.array(identity_map)
    swap_map[1, 1] = 2
    swap_map[2, 1] = 1

    identity_flips = find_atom_map_chiral_conflicts(identity_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.FLIP)
    identity_undefineds = find_atom_map_chiral_conflicts(
        identity_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.UNDEFINED
    )
    assert len(identity_flips) == 0
    assert len(identity_undefineds) == 0

    swap_map_flips = find_atom_map_chiral_conflicts(swap_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.FLIP)
    swap_map_undefineds = find_atom_map_chiral_conflicts(
        swap_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.UNDEFINED
    )
    assert len(swap_map_flips) == 8  # TODO: deduplicate idxs?
    assert len(swap_map_undefineds) == 0


def test_chiral_conflict_undefined():
    # exercise case where atom chirality is defined in one endstate, undefined in other

    mol_a = Chem.MolFromMolBlock(molblock_C, removeHs=False)
    mol_b = Chem.MolFromMolBlock(molblock_N, removeHs=False)

    conf_a = mol_a.GetConformer(0).GetPositions()
    conf_b = mol_b.GetConformer(0).GetPositions()

    chiral_set_a = ChiralRestrIdxSet.from_mol(mol_a, conf_a)
    chiral_set_b = ChiralRestrIdxSet.from_mol(mol_b, conf_b)

    assert len(chiral_set_a.restr_idxs) == 4
    assert len(chiral_set_b.restr_idxs) == 0

    partial_map = np.array([(i, i) for i in range(4)])
    partial_map_flips = find_atom_map_chiral_conflicts(
        partial_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.FLIP
    )
    partial_map_undefineds = find_atom_map_chiral_conflicts(
        partial_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.UNDEFINED
    )
    assert len(partial_map_flips) == 0
    assert len(partial_map_undefineds) == 1


def test_chiral_conflict_mixed():
    # test case containing both a flip and a partial undefined

    mol_a = Chem.MolFromMolBlock(molblock_CC, removeHs=False)
    mol_b = Chem.MolFromMolBlock(molblock_CN, removeHs=False)

    conf_a = mol_a.GetConformer(0).GetPositions()
    conf_b = mol_b.GetConformer(0).GetPositions()

    chiral_set_a = ChiralRestrIdxSet.from_mol(mol_a, conf_a)
    chiral_set_b = ChiralRestrIdxSet.from_mol(mol_b, conf_b)

    assert len(chiral_set_a.restr_idxs) == 8
    assert len(chiral_set_b.restr_idxs) == 4

    mixed_map = np.array([[i, i] for i in range(mol_b.GetNumAtoms())])

    # swap any pair of atoms around a tetrahedral center to flip chirality
    mixed_map[2, 0] = 3
    mixed_map[3, 0] = 2

    mixed_map_flips = find_atom_map_chiral_conflicts(mixed_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.FLIP)
    mixed_map_undefineds = find_atom_map_chiral_conflicts(
        mixed_map, chiral_set_a, chiral_set_b, mode=ChiralCheckMode.UNDEFINED
    )

    assert len(mixed_map_flips) == 8
    assert len(mixed_map_undefineds) == 1
