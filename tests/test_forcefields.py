import os
from glob import glob
from pathlib import Path
from warnings import catch_warnings

import numpy as np
import pytest
from common import temporary_working_dir

from timemachine import constants
from timemachine.ff import Forcefield
from timemachine.ff.handlers.deserialize import deserialize_handlers

pytestmark = [pytest.mark.nocuda]


def test_serialization_of_ffs():
    for path in glob("timemachine/ff/params/smirnoff_*.py"):
        handlers, protein_ff, water_ff = deserialize_handlers(open(path).read())
        ff = Forcefield.from_handlers(handlers, protein_ff=protein_ff, water_ff=water_ff)
        assert ff.protein_ff == constants.DEFAULT_PROTEIN_FF
        assert ff.water_ff == constants.DEFAULT_WATER_FF
        for handle in ff.get_ordered_handles():
            assert handle is not None, f"{path} failed to deserialize correctly"


def test_loading_forcefield_from_file():
    builtin_ffs = glob("timemachine/ff/params/smirnoff_*.py")
    for path in builtin_ffs:
        # Use the full path
        ff = Forcefield.load_from_file(path)
        assert ff is not None
        # Use full path as Path object
        ff = Forcefield.load_from_file(Path(path))
        assert ff is not None
        with catch_warnings(record=True) as w:
            # Load using just file name of the built in
            ff = Forcefield.load_from_file(os.path.basename(path))
            assert ff is not None
        assert len(w) == 0

    for prefix in ["", "timemachine/ff/params/"]:
        path = os.path.join(prefix, "nosuchfile.py")
        with pytest.raises(ValueError) as e:
            Forcefield.load_from_file(path)
        assert path in str(e.value)
        with pytest.raises(ValueError):
            Forcefield.load_from_file(Path(path))
        assert path in str(e.value)

    with temporary_working_dir() as tempdir:
        # Verify that if a local file shadows a builtin
        for path in builtin_ffs:
            basename = os.path.basename(path)
            with open(basename, "w") as ofs:
                ofs.write("junk")
            with catch_warnings(record=True) as w:
                Forcefield.load_from_file(basename)
            assert len(w) == 1
            assert basename in str(w[0].message)
        with catch_warnings(record=True) as w:
            bad_ff = Path(tempdir, "jut.py")
            assert bad_ff.is_absolute(), "Must be absolute to cover test case"
            with open(bad_ff, "w") as ofs:
                ofs.write("{}")
            with pytest.raises(ValueError, match="Unsupported charge handler"):
                Forcefield.load_from_file(bad_ff)
        assert len(w) == 0


def test_load_default():
    """assert that load_default() is an alias for load_from_file(DEFAULT_FF)"""

    ref = Forcefield.load_from_file(constants.DEFAULT_FF)
    test = Forcefield.load_default()

    # ref == test evaluates to false, so assert equality of smirks lists and parameter arrays manually

    ref_handles = ref.get_ordered_handles()
    test_handles = test.get_ordered_handles()

    assert len(ref_handles) == len(test_handles)

    for (ref_handle, test_handle) in zip(ref.get_ordered_handles(), test.get_ordered_handles()):
        assert ref_handle.smirks == test_handle.smirks
        np.testing.assert_array_equal(ref_handle.params, test_handle.params)
