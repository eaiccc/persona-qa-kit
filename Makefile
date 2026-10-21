.PHONY: help test check example clean

help:
	@echo "make test      run the test suite"
	@echo "make check     schema self-check + gate the example runs"
	@echo "make example   show the worked example's gate output and the cross-run report"
	@echo "make clean     remove generated runs and caches"

test:
	@cd tests && python3 -m unittest discover -s . -p 'test_*.py'

check:
	@./bin/pqa validate --self-check
	@./bin/pqa gate examples/runs/2026-08-15-notes-app
	@./bin/pqa gate examples/runs/2026-08-22-notes-app-offline
	@./bin/pqa check-improvements examples/improvements.json examples/runs

example:
	@./bin/pqa gate examples/runs/2026-08-15-notes-app
	@echo
	@./bin/pqa drift examples/runs/2026-08-15-notes-app
	@echo
	@./bin/pqa report examples/runs

clean:
	@rm -rf runs/
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	@find . -name '*.pyc' -delete
