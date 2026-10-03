"""Entry point: python -m wa2sip"""

import uvicorn

from .settings import Settings
from .web.app import create_app


def main() -> None:
    settings = Settings()
    app = create_app(settings)
    # proxy headers are handled inside the app, for trusted proxies only (WA2SIP_TRUSTED_PROXIES)
    uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_config=None,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
