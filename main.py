from __future__ import annotations

from qums_bot.app import create_app
from qums_bot.config import load_settings


app = create_app()


if __name__ == "__main__":
    settings = load_settings()
    if settings.use_waitress:
        from waitress import serve

        serve(
            app,
            host=settings.flask_host,
            port=settings.flask_port,
            threads=settings.waitress_threads,
        )
    else:
        app.run(host=settings.flask_host, port=settings.flask_port, debug=False)
