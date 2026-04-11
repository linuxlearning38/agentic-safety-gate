import requests
import sys


def test_health():
    try:
        resp = requests.get("https://localhost:5443/health", verify=False)
        assert resp.status_code == 200
        print("Health check passed")
        return True
    except Exception as e:
        print(f"Health check failed: {e}")
        return False


if __name__ == "__main__":
    success = test_health()
    sys.exit(0 if success else 1)
