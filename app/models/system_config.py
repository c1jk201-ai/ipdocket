from app.extensions import db


class SystemConfig(db.Model):
    __tablename__ = "system_config"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)  # store JSON or plain text

    @classmethod
    def get_config(cls, key: str, default: str = None) -> str:
        """Get a configuration value by key."""
        cfg = cls.query.filter_by(key=key).first()
        return cfg.value if cfg else default

    @classmethod
    def set_config(cls, key: str, value: str) -> None:
        """Set a configuration value by key (no commit; caller owns the transaction)."""
        cfg = cls.query.filter_by(key=key).first()
        if cfg:
            cfg.value = value
        else:
            cfg = cls(key=key, value=value)
            db.session.add(cfg)
