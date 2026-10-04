"""Load the macOS native kernel and verify one rolling window operation."""

from ctypes import CDLL, POINTER, c_double, c_int64, c_int
from math import isnan
from pathlib import Path

library = Path(__file__).resolve().parents[2] / "backend/native/librolling.dylib"
lib = CDLL(str(library))
lib.rolling_sum.argtypes = [POINTER(c_double), c_int64, c_int64, POINTER(c_double)]
lib.rolling_sum.restype = c_int
values = (c_double * 4)(1.0, 2.0, 3.0, 4.0)
result = (c_double * 4)()
assert lib.rolling_sum(values, 4, 2, result) == 0
assert isnan(result[0]) and list(result)[1:] == [3.0, 5.0, 7.0]
print("native rolling_sum smoke passed")
