# Thin wrapper: everything is implemented in tools/fs.py so Windows (no make) can run `python tools/fs.py <cmd>` identically.
PY ?= uv run python
.PHONY: up bootstrap down logs ps migrate topics certs realm gen seed test lint fmt evidence
up:        ; $(PY) tools/fs.py up
bootstrap: ; $(PY) tools/fs.py bootstrap
down:      ; $(PY) tools/fs.py down
logs:      ; $(PY) tools/fs.py logs
ps:        ; $(PY) tools/fs.py ps
migrate:   ; $(PY) tools/fs.py migrate
topics:    ; $(PY) tools/fs.py topics
certs:     ; $(PY) tools/gen_certs.py
realm:     ; $(PY) tools/gen_realm.py
gen:       ; $(PY) tools/gen_proto.py
seed:      ; $(PY) -m fs_simulator.seed --profile $${PROFILE:-lite}
test:      ; uv run pytest --cov --cov-report=term-missing --cov-report=xml
lint:      ; uv run ruff check .
fmt:       ; uv run ruff check --fix . && uv run ruff format .
