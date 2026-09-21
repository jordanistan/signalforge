PYTHON ?= .venv/bin/python

.PHONY: setup test test-unit check verify demo doctor
setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -q

test-unit:
	$(PYTHON) -m pytest -q --ignore=tests/test_web_e2e.py

check:
	$(PYTHON) -m compileall -q tools tests
	@for script in tools/*.sh; do bash -n "$$script" || exit; done
	@for script in web/*.js; do node --check "$$script" || exit; done

verify: check test

demo:
	$(PYTHON) tools/meshweb.py --demo

doctor:
	$(PYTHON) tools/doctor.py
