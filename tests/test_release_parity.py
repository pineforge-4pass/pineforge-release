"""Register the parity source suites with the existing test_release_* glob."""
from test_parity_bundle import ParityBundleTests
from test_parity_source_proof import ParitySourceProofTests

__all__ = ("ParityBundleTests", "ParitySourceProofTests")
