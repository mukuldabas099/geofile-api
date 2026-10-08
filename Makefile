.PHONY: install run test lint fmt demo docker

install:            ## create venv + install dev deps
	python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

run:                ## start the API with auto-reload on :8000
	uvicorn app.main:app --reload

test:               ## run the test-suite
	pytest -q

lint:               ## static checks
	ruff check .

fmt:                ## auto-fix lint + import order
	ruff check . --fix

demo:               ## upload both sample files to a running server and print the measurements
	@for f in sample_data/sample.kml sample_data/parcels_utm43n.zip; do \
	  id=$$(curl -s -F "file=@$$f" http://localhost:8000/api/files/ | python -c "import sys,json; print(json.load(sys.stdin)['id'])"); \
	  echo "== $$f -> $$id"; curl -s http://localhost:8000/api/files/$$id/measurements/ | python -m json.tool | head -40; \
	done

docker:             ## build + run in Docker
	docker compose up --build
