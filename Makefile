PYTHON     ?= python3
SCRIPT     := jfrog-openapi-toolkit.py

SCRAPED_DIR    := jfrog-apis
NORMALIZED_DIR := .normalized_apis
OUTPUT         := jfrog-merged-api/spec.json

.PHONY: all scrape normalize merge clean help

## all: Run scrape → normalize → merge in one step
all:
	$(PYTHON) $(SCRIPT) all \
		--scraped-dir $(SCRAPED_DIR) \
		--normalized-dir $(NORMALIZED_DIR) \
		--output $(OUTPUT)

## scrape: Download raw OpenAPI specs from JFrog docs
scrape:
	$(PYTHON) $(SCRIPT) scrape --output-dir $(SCRAPED_DIR)

## normalize: Absorb server URL paths into API path keys
normalize:
	$(PYTHON) $(SCRIPT) normalize \
		--input-dir $(SCRAPED_DIR) \
		--output-dir $(NORMALIZED_DIR)

## merge: Merge normalized specs into a single JSON file
merge:
	$(PYTHON) $(SCRIPT) merge \
		--input-dir $(NORMALIZED_DIR) \
		--output $(OUTPUT)

## clean: Remove generated directories and output file
clean:
	rm -rf $(SCRAPED_DIR) $(NORMALIZED_DIR) .merge-tmp $(OUTPUT)

## help: Show this help message
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
