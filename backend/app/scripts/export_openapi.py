"""Export the OpenAPI schema to stdout.

Used to generate TypeScript types for the frontend without a running server:
    python -m app.scripts.export_openapi > openapi.json
    (then in frontend/: npm run openapi)
"""

import json
import sys

from app.main import app


def main() -> None:
    json.dump(app.openapi(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
