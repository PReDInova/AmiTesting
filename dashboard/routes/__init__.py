"""
Blueprint registration helper for the AmiTesting dashboard.

Imports all blueprint modules and provides a single ``register_blueprints``
function that the main ``app.py`` calls during startup.
"""

from dashboard.routes.backtest import backtest_bp
from dashboard.routes.strategy import strategy_bp
from dashboard.routes.live import live_bp
from dashboard.routes.indicators import indicators_bp
from dashboard.routes.batch import batch_bp
from dashboard.routes.trades import trades_bp
from dashboard.routes.data_api import data_api_bp


def register_blueprints(app):
    """Register all route blueprints on the Flask app."""
    app.register_blueprint(backtest_bp)
    app.register_blueprint(strategy_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(indicators_bp)
    app.register_blueprint(batch_bp)
    app.register_blueprint(trades_bp)
    app.register_blueprint(data_api_bp)
