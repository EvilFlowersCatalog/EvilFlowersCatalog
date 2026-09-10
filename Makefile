.PHONY: lcp-compliance

# IP-003 Phase 5 — LCP compliance test target.
# Runs the Django test runner over the readium and opds2 apps.
lcp-compliance:
	python manage.py test apps.readium.tests apps.opds2 --verbosity=2
