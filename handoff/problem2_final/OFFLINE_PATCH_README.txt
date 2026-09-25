Problem 2 final offline patch (2026-09-25)
============================================================

Purpose
-------
This patch is for a Linux server with no Git executable and no network.
It contains only source, tests, and run instructions. It does not contain
data/ or artifacts/, and it must not replace those directories.

Why this patch is needed
------------------------
The server test log showed 72 tests rather than the expected 77 tests. It
also combined the new offline-runtime test with the old audit implementation,
so FileNotFoundError("git") was not treated as optional metadata. This is a
mixed source snapshot, not a requirement to install Git.

Installation from the repository code directory
------------------------------------------------
1. Upload problem2_final_offline_patch_20260925.zip into the code directory.
2. Back up the existing source files if desired.
3. Extract the archive over the code directory. With Python only:

   python - <<'PY'
   import zipfile
   with zipfile.ZipFile('problem2_final_offline_patch_20260925.zip') as z:
       z.extractall('.')
   PY

4. Do not delete or replace data/ or artifacts/.
5. Run:

   python -m unittest discover -s tests -v

Expected result: 77 tests, OK, exit code 0. The server does not need Git.

Critical file SHA-256 values after extraction
---------------------------------------------
35a7a19007ed12c8136d05694149f092b227be172537e8dc99213b615235dd94  candidate_manager_problem2_final.py
2e7e3dc61db99906349c1ea08af4e99d641d64c6e0fa9587c22736acbdbe2790  run_problem2_final.py
7936e8a148ee2f97051e34504da0d3aa9f5ccf48d770c0996a5bf320e27d2f3c  problem2_preregistered_split.json
10ab37413caf5bf8a3b79e09bfe32c874cb5ef4054c859e172e5bdfce75915a2  audit_problem3_assets.py
a809a3887a55ab9cfb53ba1a6b7c76dba7b0a074452dac4240c5696c717f323f  run_problem3_g1.py
1904cd3cbcf3971cccb1478b2df1267f64aa6ece52a2c1f0ab26651cfb0679ec  tests/test_problem3_offline_runtime.py
77e617385ee4c4afec7883a495676f8179ade4c49bfb2f777710cb9db001d629  tests/test_candidate_manager_problem2_final.py

Server smoke test
-----------------
After the 77 tests pass, run a new environment-local smoke test:

   python -u run_problem2_final.py \
     --repo . \
     --baseline artifacts/problem2_stage1_baseline_cold_audit \
     --output artifacts/problem2_final_server_smoke \
     --cache /media/dell/data/yn/problem2_final_server_smoke_cache \
     --phase custom \
     --cases case_001 \
     --max-cores 3 \
     --workers 1

Expected result: complete=true, recorded_case_core_cells=3,
candidate_failures=0, fatal_failures=0.

Full run
--------
Only after the smoke test passes:

   python -u run_problem2_final.py \
     --repo . \
     --baseline artifacts/problem2_stage1_baseline_cold_audit \
     --output artifacts/problem2_final_v1 \
     --cache /media/dell/data/yn/problem2_final_cache_v1 \
     --phase all \
     --workers 24

