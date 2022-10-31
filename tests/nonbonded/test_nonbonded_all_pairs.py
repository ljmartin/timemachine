import numpy as np
import pytest
from common import GradientTest, gen_nonbonded_params_with_4d_offsets

from timemachine.lib.potentials import NonbondedAllPairs
from timemachine.potentials import generic

pytestmark = [pytest.mark.memcheck]


def test_nonbonded_all_pairs_invalid_atom_idxs():
    with pytest.raises(RuntimeError) as e:
        NonbondedAllPairs(2, 2.0, 1.1, [0, 0]).unbound_impl(np.float32)

    assert "atom indices must be unique" in str(e)


def test_nonbonded_all_pairs_invalid_num_atoms():
    potential = NonbondedAllPairs(1, 2.0, 1.1).unbound_impl(np.float32)
    with pytest.raises(RuntimeError) as e:
        potential.execute(np.zeros((2, 3)), np.zeros((1, 3)), np.eye(3))

    assert "NonbondedAllPairs::execute_device(): expected N == N_, got N=2, N_=1" in str(e)


def test_nonbonded_all_pairs_invalid_num_params():
    potential = NonbondedAllPairs(1, 2.0, 1.1).unbound_impl(np.float32)
    with pytest.raises(RuntimeError) as e:
        potential.execute(np.zeros((1, 3)), np.zeros((2, 3)), np.eye(3))

    assert "NonbondedAllPairs::execute_device(): expected P == N_*4, got P=6, N_*4=4" in str(e)


def test_nonbonded_all_pairs_singleton_subset(rng: np.random.Generator):
    """Checks that energy and derivatives are all zero when called with a single-atom subset"""
    num_atoms = 231
    beta = 2.0
    cutoff = 1.1
    box = 3.0 * np.eye(3)
    conf = rng.uniform(0, 1, size=(num_atoms, 3))
    params = rng.uniform(0, 1, size=(num_atoms, 4))

    for idx in rng.choice(num_atoms, size=(10,)):
        atom_idxs = np.array([idx], dtype=np.int32)
        potential = NonbondedAllPairs(num_atoms, beta, cutoff, atom_idxs)
        du_dx, du_dp, u = potential.unbound_impl(np.float64).execute(conf, params, box)

        assert (du_dx == 0).all()
        assert (du_dp == 0).all()
        assert u == 0


def test_nonbonded_all_pairs_improper_subset(rng: np.random.Generator):
    """Checks for bitwise equivalence of the following cases:
    1. atom_idxs = None
    2. atom_idxs = range(num_atoms)
    """
    num_atoms = 231
    beta = 2.0
    cutoff = 1.1
    box = 3.0 * np.eye(3)
    conf = rng.uniform(0, 1, size=(num_atoms, 3))
    params = rng.uniform(0, 1, size=(num_atoms, 4))

    def test_impl(atom_idxs):
        return NonbondedAllPairs(num_atoms, beta, cutoff, atom_idxs).unbound_impl(np.float64).execute(conf, params, box)

    du_dx_1, du_dp_1, u_1 = test_impl(None)
    du_dx_2, du_dp_2, u_2 = test_impl(np.arange(num_atoms, dtype=np.int32))

    np.testing.assert_array_equal(du_dx_1, du_dx_2)
    np.testing.assert_array_equal(du_dp_1, du_dp_2)
    assert u_1 == u_2


@pytest.mark.parametrize("beta", [2.0])
@pytest.mark.parametrize("cutoff", [1.1])
@pytest.mark.parametrize("precision,rtol,atol", [(np.float64, 1e-8, 1e-8), (np.float32, 1e-4, 5e-4)])
@pytest.mark.parametrize("num_atoms_subset", [None, 33])
@pytest.mark.parametrize("num_atoms", [33, 65, 231])
def test_nonbonded_all_pairs_correctness(
    num_atoms,
    num_atoms_subset,
    precision,
    rtol,
    atol,
    cutoff,
    beta,
    example_nonbonded_potential,
    example_conf,
    example_box,
    rng: np.random.Generator,
):
    "Compares with jax reference implementation."

    conf = example_conf[:num_atoms]
    params = example_nonbonded_potential.params[:num_atoms, :]

    atom_idxs = (
        rng.choice(num_atoms, size=(num_atoms_subset,), replace=False).astype(np.int32) if num_atoms_subset else None
    )

    potential = generic.NonbondedAllPairs(num_atoms, beta, cutoff, atom_idxs)

    GradientTest().compare_forces_gpu_vs_reference(
        conf,
        gen_nonbonded_params_with_4d_offsets(rng, params, cutoff),
        example_box,
        potential,
        precision=precision,
        rtol=rtol,
        atol=atol,
    )
