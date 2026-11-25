"""Chargement de la configuration OdooGuard.

Source unique : ``/etc/odooguard/odooguard.conf`` (format INI, droits 600,
propriétaire ``odooguard``), hors dépôt Git. Toute clé peut être surchargée
par une variable d'environnement ``ODOOGUARD_<SECTION>_<CLE>`` (en majuscules,
``:`` et ``-`` remplacés par ``_``), ce qui permet d'injecter un secret par
systemd (``LoadCredential=``/``EnvironmentFile=``) sans l'écrire dans le fichier.

Les secrets volumineux (mot de passe restic, phrase de chiffrement pgBackRest)
restent dans des fichiers séparés, en 600, référencés par leur chemin.
"""

from __future__ import annotations

import configparser
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = "/etc/odooguard/odooguard.conf"


class ConfigError(Exception):
    """Configuration absente, illisible, incomplète ou trop permissive."""


def _split(value: str) -> list[str]:
    """Liste séparée par des virgules ou des retours à la ligne."""
    return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]


# Types de notification réglables séparément (e-mail / Telegram) : clé -> libellé
NOTIFY_CATEGORIES = {
    "echec": "Échec d'une sauvegarde, d'une copie ou du versement des WAL",
    "unite": "Service OdooGuard en échec (journal systemd joint, double du précédent)",
    "etat": "État des sauvegardes : retard, flux WAL, test, Veeam, G:\\",
    "stockage": "Espace disque (OdooGuard, serveur Odoo, NAS)",
    "services": "Service arrêté (Odoo, PostgreSQL, nginx, OdooGuard...)",
    "intrusion": "Connexions Odoo échouées au-delà du seuil",
    "comptes": "Comptes Odoo créés, supprimés, archivés",
    "test": "Rapport du test de restauration hebdomadaire",
}


def notify_categories(cfg: "Config") -> dict[str, str]:
    """Types de notification : les fixes, puis un par NAS (accès et copies vers ce NAS)."""
    cats = dict(NOTIFY_CATEGORIES)
    for d in cfg.destinations.values():
        where = f" ({d.share})" if d.share else ""
        cats[f"nas-{d.name}"] = (f"NAS {d.name}{where} : accès, copies et échecs de copie"
                                 + ("" if d.enabled else " - suspendu"))
    return cats


@dataclass
class Target:
    """Machine sur laquelle OdooGuard sait restaurer (server01, VM de test...)."""

    name: str
    host: str
    ssh_user: str = "root"
    ssh_key: str = "/etc/odooguard/ssh/id_agent"
    pg_version: str = "15"
    pg_cluster: str = "main"
    pg_datadir: str = ""
    odoo_service: str = "odoo"
    odoo_conf: str = "/etc/odoo/odoo.conf"
    odoo_url: str = ""
    filestore_path: str = "/var/lib/odoo/.local/share/Odoo/filestore"
    is_production: bool = False

    def __post_init__(self) -> None:
        if not self.pg_datadir:
            self.pg_datadir = f"/var/lib/postgresql/{self.pg_version}/{self.pg_cluster}"


@dataclass
class Destination:
    """Copie de second niveau (NAS principal, NAS secondaire...)."""

    name: str
    path: str  # répertoire racine OdooGuard sur la destination
    mountpoint: str = ""  # doit être un point de montage actif (sinon : alerte)
    min_free_gb: float = 100.0
    restic_password_file: str = ""  # vide = même mot de passe que le dépôt local
    enabled: bool = True
    # Renseignés quand le NAS est géré depuis le tableau de bord (page « NAS »)
    share: str = ""  # //192.168.8.30/odoo
    subdir: str = "odooguard"
    username: str = ""
    domain: str = ""
    smb_version: str = "3.0"


@dataclass
class External:
    """Composant extérieur qui rend compte de son état à OdooGuard (Veeam, copie G:\\)."""

    name: str
    label: str
    max_age: int = 26 * 3600  # au-delà sans rapport réussi : critique
    token_file: str = ""  # jeton d'envoi (Bearer), fichier 600
    enabled: bool = True


@dataclass
class AlertSettings:
    channels: list[str] = field(default_factory=lambda: ["journal"])
    min_level: str = "warning"  # niveau minimal notifié
    repeat_hours: float = 4.0  # rappel tant que le problème persiste
    send_resolved: bool = True
    # E-mail
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"  # starttls | ssl | none
    smtp_user: str = ""
    smtp_password_file: str = ""
    mail_from: str = ""
    mail_to: list[str] = field(default_factory=list)
    # Telegram
    telegram_token_file: str = ""
    telegram_chat_ids: list[str] = field(default_factory=list)
    telegram_api: str = "https://api.telegram.org"
    https_proxy: str = ""
    # Envoi par type de notification : catégorie -> canaux (sous-ensemble de « channels »).
    # Absent = tous les canaux activés. Liste vide = journal système seulement.
    routes: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class WebSettings:
    listen: str = "0.0.0.0"
    port: int = 8443
    certfile: str = "/etc/odooguard/web/odooguard.crt"
    keyfile: str = "/etc/odooguard/web/odooguard.key"
    users_file: str = "/etc/odooguard/web/users"
    secret_file: str = "/etc/odooguard/web/secret"
    session_hours: float = 8.0
    public_status: str = "/var/lib/odooguard-public/status.json"


@dataclass
class RestoreTestSettings:
    """Test de restauration automatique hebdomadaire (phase 3)."""

    target: str = "testvm"
    databases: list[str] = field(default_factory=list)  # vide = [odoo] databases
    point: str = "hier 16:00"  # instant restauré (PITR)
    keep_last: int = 1  # bases de test conservées pour consultation
    tolerance: float = 0.10  # écart toléré des comptages par rapport à la production
    max_staleness_days: float = 7.0  # dernière activité trop ancienne => avertissement
    start_odoo: bool = True
    http_timeout: int = 300
    max_age: int = 8 * 86400  # au-delà sans test réussi : critique


@dataclass
class MonitoringSettings:
    """Surveillance : espace disque, services, sécurité d'Odoo (section [monitoring])."""

    disk_warn_pct: float = 80.0  # occupation à partir de laquelle on avertit
    disk_crit_pct: float = 90.0  # ... et à partir de laquelle c'est critique
    services_local: list[str] = field(default_factory=lambda: [
        "odooguard-walreceiver.service", "odooguard-web.service", "odooguard-jobs.service"])
    security: bool = True  # lecture des connexions et des comptes Odoo
    failed_logins_warn: int = 10  # échecs de connexion dans la fenêtre => avertissement
    failed_logins_window: int = 15  # minutes
    notify_accounts: bool = True  # prévenir à chaque création / suppression de compte Odoo
    keep_days: int = 180  # conservation des événements de sécurité


@dataclass
class Config:
    path: str
    site: str = "LNHC"
    state_dir: str = "/var/lib/odooguard"
    log_dir: str = "/var/log/odooguard"
    lock_dir: str = "/run/odooguard"
    staging_dir: str = "/srv/odooguard/staging"
    min_free_gb: float = 50.0
    run_as: str = "odooguard"

    # Serveur Odoo de production (source des sauvegardes)
    source: Target = field(default_factory=lambda: Target("odoo", "192.168.8.80"))
    databases: list[str] = field(default_factory=list)
    file_sources: list[str] = field(default_factory=list)
    rsync_bwlimit_day: str = "40M"  # limite de débit 08h-18h (impact Odoo)
    rsync_excludes: list[str] = field(default_factory=list)

    # PostgreSQL / pgBackRest
    stanza: str = "lnhc"
    pgbackrest_bin: str = "pgbackrest"
    pgbackrest_config: str = "/etc/pgbackrest/pgbackrest.conf"
    ingest_config: str = "/etc/pgbackrest/odooguard-ingest.conf"
    repo_path: str = "/srv/odooguard/pgbackrest"
    wal_spool: str = "/srv/odooguard/wal-spool"
    pg_shadow: str = "/var/lib/odooguard/pgshadow"
    wal_keep_local: int = 256  # segments déjà archivés conservés dans le spool
    wal_receiver_unit: str = "odooguard-walreceiver.service"
    pg_bin_dir: str = ""  # /usr/lib/postgresql/<ver>/bin

    # restic
    restic_bin: str = "restic"
    restic_repo: str = "/srv/odooguard/restic"
    restic_password_file: str = "/etc/odooguard/restic.pass"
    mirror_dir: str = "/srv/odooguard/mirror"
    restic_keep: dict[str, str] = field(default_factory=dict)

    # Seuils de supervision (secondes)
    max_age: dict[str, int] = field(default_factory=dict)

    targets: dict[str, Target] = field(default_factory=dict)
    destinations: dict[str, Destination] = field(default_factory=dict)
    externals: dict[str, External] = field(default_factory=dict)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    web: WebSettings = field(default_factory=WebSettings)
    restore_test: RestoreTestSettings = field(default_factory=RestoreTestSettings)
    monitoring: MonitoringSettings = field(default_factory=MonitoringSettings)

    # ------------------------------------------------------------------ accès
    def target(self, name: str) -> Target:
        if name in ("odoo", self.source.name):
            return self.source
        try:
            return self.targets[name]
        except KeyError:
            raise ConfigError(
                f"cible inconnue « {name} » (connues : odoo, {', '.join(self.targets) or '-'})"
            ) from None

    @property
    def history_db(self) -> str:
        return os.path.join(self.state_dir, "history.db")

    @property
    def status_file(self) -> str:
        return os.path.join(self.state_dir, "status.json")

    @property
    def audit_log(self) -> str:
        return os.path.join(self.log_dir, "audit.log")


def _env_override(section: str, key: str) -> str | None:
    name = f"ODOOGUARD_{section}_{key}".upper().replace(":", "_").replace("-", "_")
    return os.environ.get(name)


class _Reader:
    """Petit utilitaire : lecture typée avec surcharge par l'environnement."""

    def __init__(self, cp: configparser.ConfigParser):
        self.cp = cp

    def get(self, section: str, key: str, default: str | None = None) -> str | None:
        env = _env_override(section, key)
        if env is not None:
            return env
        if self.cp.has_option(section, key):
            return self.cp.get(section, key)
        return default

    def req(self, section: str, key: str) -> str:
        value = self.get(section, key)
        if value in (None, ""):
            raise ConfigError(f"[{section}] {key} est obligatoire")
        return value

    def int(self, section: str, key: str, default: int) -> int:
        return int(self.get(section, key, str(default)))

    def float(self, section: str, key: str, default: float) -> float:
        return float(self.get(section, key, str(default)))

    def bool(self, section: str, key: str, default: bool) -> bool:
        raw = self.get(section, key, "yes" if default else "no")
        return str(raw).strip().lower() in ("1", "yes", "true", "on", "oui", "y")


def _target_from_section(r: _Reader, section: str, name: str) -> Target:
    t = Target(
        name=name,
        host=r.req(section, "host"),
        ssh_user=r.get(section, "ssh_user", "root"),
        ssh_key=r.get(section, "ssh_key", "/etc/odooguard/ssh/id_agent"),
        pg_version=r.get(section, "pg_version", "15"),
        pg_cluster=r.get(section, "pg_cluster", "main"),
        pg_datadir=r.get(section, "pg_datadir", ""),
        odoo_service=r.get(section, "odoo_service", "odoo"),
        odoo_conf=r.get(section, "odoo_conf", "/etc/odoo/odoo.conf"),
        odoo_url=r.get(section, "odoo_url", ""),
        filestore_path=r.get(
            section, "filestore_path", "/var/lib/odoo/.local/share/Odoo/filestore"
        ),
        is_production=r.bool(section, "is_production", False),
    )
    return t


def check_permissions(path: str) -> None:
    """Refuse un fichier de configuration lisible par le groupe ou les autres."""
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"{path} a des droits trop ouverts ({stat.filemode(st.st_mode)}) : "
            f"exécuter « chmod 600 {path} »"
        )


def load(path: str | None = None, *, strict_perms: bool = True) -> Config:
    path = path or os.environ.get("ODOOGUARD_CONFIG", DEFAULT_CONFIG)
    if not Path(path).is_file():
        raise ConfigError(f"fichier de configuration introuvable : {path}")
    if strict_perms:
        check_permissions(path)

    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"{path} illisible : {exc}") from None
    r = _Reader(cp)

    cfg = Config(path=path)
    cfg.site = r.get("general", "site", cfg.site)
    cfg.state_dir = r.get("general", "state_dir", cfg.state_dir)
    cfg.log_dir = r.get("general", "log_dir", cfg.log_dir)
    cfg.lock_dir = r.get("general", "lock_dir", cfg.lock_dir)
    cfg.staging_dir = r.get("general", "staging_dir", cfg.staging_dir)
    cfg.min_free_gb = r.float("general", "min_free_gb", cfg.min_free_gb)
    cfg.run_as = r.get("general", "run_as", cfg.run_as)

    cfg.source = _target_from_section(r, "odoo", "odoo")
    cfg.source.is_production = True
    cfg.databases = _split(r.req("odoo", "databases"))
    cfg.file_sources = _split(r.req("odoo", "file_sources"))
    cfg.rsync_bwlimit_day = r.get("odoo", "rsync_bwlimit_day", cfg.rsync_bwlimit_day)
    cfg.rsync_excludes = _split(r.get("odoo", "rsync_excludes", "") or "")

    cfg.stanza = r.get("pgbackrest", "stanza", cfg.stanza)
    cfg.pgbackrest_bin = r.get("pgbackrest", "bin", cfg.pgbackrest_bin)
    cfg.pgbackrest_config = r.get("pgbackrest", "config", cfg.pgbackrest_config)
    cfg.ingest_config = r.get("pgbackrest", "ingest_config", cfg.ingest_config)
    cfg.repo_path = r.get("pgbackrest", "repo_path", cfg.repo_path)
    cfg.wal_spool = r.get("pgbackrest", "wal_spool", cfg.wal_spool)
    cfg.pg_shadow = r.get("pgbackrest", "pg_shadow", cfg.pg_shadow)
    cfg.wal_keep_local = r.int("pgbackrest", "wal_keep_local", cfg.wal_keep_local)
    cfg.wal_receiver_unit = r.get("pgbackrest", "wal_receiver_unit", cfg.wal_receiver_unit)
    cfg.pg_bin_dir = r.get(
        "pgbackrest", "pg_bin_dir", f"/usr/lib/postgresql/{cfg.source.pg_version}/bin"
    )

    cfg.restic_bin = r.get("restic", "bin", cfg.restic_bin)
    cfg.restic_repo = r.get("restic", "repository", cfg.restic_repo)
    cfg.restic_password_file = r.get("restic", "password_file", cfg.restic_password_file)
    cfg.mirror_dir = r.get("restic", "mirror_dir", cfg.mirror_dir)
    cfg.restic_keep = {
        k: r.get("restic", f"keep_{k}", d)
        for k, d in (("hourly", "48"), ("daily", "14"), ("weekly", "8"), ("monthly", "12"))
    }

    cfg.max_age = {
        "db_full": r.int("thresholds", "db_full_max_age", 8 * 86400),
        "db_any": r.int("thresholds", "db_any_max_age", 26 * 3600),
        "wal": r.int("thresholds", "wal_max_lag", 15 * 60),
        "files": r.int("thresholds", "files_max_age", 26 * 3600),
        "copy": r.int("thresholds", "copy_max_age", 3 * 3600),
    }

    a = cfg.alerts
    a.channels = _split(r.get("alerts", "channels", "journal") or "journal")
    a.min_level = r.get("alerts", "min_level", a.min_level)
    a.repeat_hours = r.float("alerts", "repeat_hours", a.repeat_hours)
    a.send_resolved = r.bool("alerts", "send_resolved", True)
    a.smtp_host = r.get("smtp", "host", "") or ""
    a.smtp_port = r.int("smtp", "port", a.smtp_port)
    a.smtp_security = r.get("smtp", "security", a.smtp_security)
    a.smtp_user = r.get("smtp", "user", "") or ""
    a.smtp_password_file = r.get("smtp", "password_file", "") or ""
    a.mail_from = r.get("smtp", "from", "") or ""
    a.mail_to = _split(r.get("smtp", "to", "") or "")
    a.telegram_token_file = r.get("telegram", "token_file", "") or ""
    a.telegram_chat_ids = _split(r.get("telegram", "chat_ids", "") or "")
    a.telegram_api = r.get("telegram", "api", a.telegram_api)
    a.https_proxy = r.get("telegram", "https_proxy", "") or ""
    for cat in NOTIFY_CATEGORIES:
        raw = r.get("alerts", f"route_{cat}")
        if raw is not None:
            a.routes[cat] = [c for c in _split(raw) if c in ("email", "telegram")]

    w = cfg.web
    w.listen = r.get("web", "listen", w.listen)
    w.port = r.int("web", "port", w.port)
    w.certfile = r.get("web", "certfile", w.certfile)
    w.keyfile = r.get("web", "keyfile", w.keyfile)
    w.users_file = r.get("web", "users_file", w.users_file)
    w.secret_file = r.get("web", "secret_file", w.secret_file)
    w.session_hours = r.float("web", "session_hours", w.session_hours)
    w.public_status = r.get("web", "public_status", w.public_status)

    t = cfg.restore_test
    t.target = r.get("restore_test", "target", t.target)
    t.databases = _split(r.get("restore_test", "databases", "") or "") or list(cfg.databases)
    t.point = r.get("restore_test", "point", t.point)
    t.keep_last = r.int("restore_test", "keep_last", t.keep_last)
    t.tolerance = r.float("restore_test", "tolerance", t.tolerance)
    t.max_staleness_days = r.float("restore_test", "max_staleness_days", t.max_staleness_days)
    t.start_odoo = r.bool("restore_test", "start_odoo", True)
    t.http_timeout = r.int("restore_test", "http_timeout", t.http_timeout)
    t.max_age = r.int("restore_test", "max_age", t.max_age)

    m = cfg.monitoring
    m.disk_warn_pct = r.float("monitoring", "disk_warn_pct", m.disk_warn_pct)
    m.disk_crit_pct = r.float("monitoring", "disk_crit_pct", m.disk_crit_pct)
    m.services_local = _split(r.get("monitoring", "services_local", "") or "") or m.services_local
    m.security = r.bool("monitoring", "security", m.security)
    m.failed_logins_warn = r.int("monitoring", "failed_logins_warn", m.failed_logins_warn)
    m.failed_logins_window = r.int("monitoring", "failed_logins_window", m.failed_logins_window)
    m.notify_accounts = r.bool("monitoring", "notify_accounts", m.notify_accounts)
    m.keep_days = r.int("monitoring", "keep_days", m.keep_days)
    if not 1 <= m.disk_warn_pct < m.disk_crit_pct <= 100:
        raise ConfigError("[monitoring] il faut 1 ≤ disk_warn_pct < disk_crit_pct ≤ 100")

    for section in cp.sections():
        if section.startswith("target:"):
            name = section.split(":", 1)[1]
            cfg.targets[name] = _target_from_section(r, section, name)
        elif section.startswith("destination:"):
            name = section.split(":", 1)[1]
            cfg.destinations[name] = Destination(
                name=name,
                path=r.req(section, "path"),
                mountpoint=r.get(section, "mountpoint", "") or "",
                min_free_gb=r.float(section, "min_free_gb", 100.0),
                restic_password_file=r.get(section, "restic_password_file", "") or "",
                enabled=r.bool(section, "enabled", True),
                share=r.get(section, "share", "") or "",
                subdir=r.get(section, "subdir", "odooguard") or "odooguard",
                username=r.get(section, "username", "") or "",
                domain=r.get(section, "domain", "") or "",
                smb_version=r.get(section, "smb_version", "3.0") or "3.0",
            )
        elif section.startswith("external:"):
            name = section.split(":", 1)[1]
            cfg.externals[name] = External(
                name=name,
                label=r.get(section, "label", name) or name,
                max_age=r.int(section, "max_age", 26 * 3600),
                token_file=r.get(section, "token_file", "") or "",
                enabled=r.bool(section, "enabled", True),
            )
    for name in cfg.destinations:  # envoi par type pour chaque NAS (connu après les sections)
        raw = r.get("alerts", f"route_nas-{name}")
        if raw is not None:
            a.routes[f"nas-{name}"] = [c for c in _split(raw) if c in ("email", "telegram")]
    return cfg
