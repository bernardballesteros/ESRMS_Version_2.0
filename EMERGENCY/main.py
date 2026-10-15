import os

from app import create_app
from app.delivery import start_worker

app = create_app()

if __name__ == "__main__":
    
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_worker(app)
    app.run(debug=True)
else:
    start_worker(app)