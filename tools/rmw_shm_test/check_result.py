"""Require payload correctness and evidence that the intended transport branch ran."""
import json
from pathlib import Path
import sys

result_path, log_path, expectation = sys.argv[1:]
result = json.loads(Path(result_path).read_text())
log = Path(log_path).read_text()
assert result["sequence"] == list(range(1, 41)) and result["frames"] == 40, result
assert result["serialized_sizes"] == [12583025], result
assert "can_loan_messages=0" in log, "Unexpected loan behavior: re-audit this environment"
allocations = log.count("Experimental CPU-group SHM allocation:")
fallbacks = log.count("CPU-group SHM exact-length heap fallback:")
if expectation == "shm":
    assert allocations > 0 and fallbacks == 0, (allocations, fallbacks)
elif expectation == "overestimate":
    assert allocations > 0 and fallbacks == allocations, (allocations, fallbacks)
    assert log.count("CPU-group SHM exact-length heap fallback: 12583025 of 12583089 bytes") == fallbacks
elif expectation in ("heap", "exhausted"):
    assert allocations == 0 and fallbacks == 0, (allocations, fallbacks)
else:
    raise ValueError(expectation)
result.update(expectation=expectation, cpu_shm_allocations=allocations,
              exact_length_fallbacks=fallbacks, passed=True)
Path(result_path).write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({key: result[key] for key in ("frames", "serialized_sizes", "expectation", "cpu_shm_allocations", "exact_length_fallbacks", "passed")}))
