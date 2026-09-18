"""mc-rcon: varios comandos por conexión, salida limpia (sin §), --json, código 2 con contraseña mala, 3 sin servidor."""
import json

from mcconsole import cli
from mcconsole.profiles import LogSpec, Profile


def test_commands_and_json(profile, fake_server, capsys, monkeypatch):
    monkeypatch.setattr(cli, "resolve", lambda name: profile)
    assert cli.main(["-p", "test", "list", "tps"]) == 0
    out = capsys.readouterr().out
    assert "# list" in out and "TPS: 20.0" in out and "§" not in out
    assert cli.main(["-p", "test", "--json", "tps"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["response"] == "TPS: 20.0"


def test_bad_password_and_no_server(profile, fake_server, tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad"; bad.write_text("nope")
    p2 = Profile(name="t", host="127.0.0.1", rcon_port=fake_server.port, password_file=str(bad), log=LogSpec())
    monkeypatch.setattr(cli, "resolve", lambda name: p2)
    assert cli.main(["list"]) == 2
    p3 = Profile(name="t", host="127.0.0.1", rcon_port=1, password_file=str(bad), log=LogSpec())
    monkeypatch.setattr(cli, "resolve", lambda name: p3)
    assert cli.main(["list"]) == 3
