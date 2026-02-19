import unittest

from bridge.web_common import same_origin_path


class SameOriginPathTests(unittest.TestCase):
    def test_matches_loopback_hosts_with_same_port(self) -> None:
        self.assertTrue(same_origin_path("http://localhost:5181/", "http://127.0.0.1:5181/"))
        self.assertTrue(same_origin_path("http://127.0.0.1:5181/", "http://localhost:5181/"))

    def test_rejects_different_ports(self) -> None:
        self.assertFalse(same_origin_path("http://localhost:5181/", "http://127.0.0.1:5173/"))

    def test_rejects_different_paths(self) -> None:
        self.assertFalse(same_origin_path("http://localhost:5181/a", "http://127.0.0.1:5181/b"))


if __name__ == "__main__":
    unittest.main()
