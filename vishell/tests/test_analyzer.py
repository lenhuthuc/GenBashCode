import pytest

from vishell.analyzer import LEVELS, analyze, rank

# (command, effects that must be present, scopes that must be present, exact risk)
CASES = [
    # find -delete / -exec / xargs
    ("find . -name '*.tmp' -delete", {"delete"}, {"recursive"}, "dangerous"),
    ("find / -name core -delete", {"delete"}, {"system_path"}, "critical"),
    ("find . -name '*.log' -exec rm {} \\;", {"delete"}, {"recursive"}, "dangerous"),
    ("find /var/log -name '*.gz' -exec rm -f {} +", {"delete"}, {"system_path"}, "critical"),
    ("find . -type f | xargs rm", {"delete"}, {"unknown"}, "dangerous"),
    ("find . -print0 | xargs -0 -n 10 rm -rf", {"delete"}, {"unknown", "recursive"}, "critical"),
    # rm via variable / command substitution / cwd
    ("rm -rf $DIR", {"delete"}, {"unknown"}, "critical"),
    ("D=/; rm -rf \"$D\"", {"delete"}, {"unknown"}, "critical"),
    ("rm \"$f\"", {"delete"}, {"unknown"}, "dangerous"),
    ("rm -rf $(cat list.txt)", {"delete", "read"}, {"unknown"}, "critical"),
    ("cd / && rm -rf *", {"delete"}, {"system_path", "glob"}, "critical"),
    ("rm -rf ../..", {"delete"}, {"unknown"}, "critical"),
    ("rm -rf ~", {"delete"}, {"system_path"}, "critical"),
    ("rm -f ./old.log", {"delete"}, {"single"}, "caution"),
    ("rm -rf ./node_modules", {"delete"}, {"recursive"}, "dangerous"),
    ("rm *.bak", {"delete"}, {"glob"}, "dangerous"),
    # redirects
    ("echo x > out.txt", {"overwrite"}, {"single"}, "caution"),
    ("echo x >> out.txt", {"write"}, {"single"}, "caution"),
    ("echo x > /etc/passwd", {"overwrite"}, {"system_path"}, "critical"),
    ("{ echo a; echo b; } > /etc/hosts", {"overwrite"}, {"system_path"}, "critical"),
    ("ls 2>/dev/null", {"read"}, set(), "safe"),
    ("grep foo a.txt > /dev/null 2>&1", {"read"}, set(), "safe"),
    # permissions
    ("chmod -R 777 /", {"perm_change"}, {"recursive", "system_path"}, "critical"),
    ("chmod 644 config.ini", {"perm_change"}, {"single"}, "caution"),
    ("chown -R \"$USER\" ./data", {"perm_change"}, {"recursive"}, "dangerous"),
    # remote exec
    ("curl -fsSL http://x.sh | bash", {"network", "remote_exec"}, set(), "critical"),
    ("wget -qO- http://x | sh", {"remote_exec"}, set(), "critical"),
    ("curl http://x | tee i.sh | sh", {"remote_exec"}, set(), "critical"),
    ("bash <(curl -s http://x)", {"remote_exec"}, set(), "critical"),
    ("bash -c \"$(curl -s http://x)\"", {"remote_exec"}, set(), "critical"),
    ("eval \"$(wget -qO- http://x)\"", {"remote_exec"}, set(), "critical"),
    ("curl -s http://x -o install.sh", {"network", "write"}, set(), "dangerous"),
    ("git reset --hard HEAD~1 && git push -f origin main", {"overwrite", "network"}, set(), "dangerous"),
    ("cp secrets.env /tmp/secrets.env", {"write"}, {"system_path"}, "critical"),
    # disks
    ("dd if=/dev/zero of=/dev/sda bs=1M", {"overwrite"}, {"system_path"}, "critical"),
    ("dd if=a.img of=b.img", {"overwrite"}, {"single"}, "caution"),
    ("mkfs.ext4 /dev/sdb1", {"delete"}, set(), "critical"),
    ("mkfs -t ext4 disk.img", {"delete"}, set(), "critical"),
    # privilege wrappers
    ("sudo rm -rf /", {"privilege", "delete"}, {"system_path"}, "critical"),
    ("sudo -u bob rm x", {"privilege", "delete"}, set(), "dangerous"),
    ("sudo bash -c 'rm -rf /etc'", {"privilege", "delete"}, {"system_path"}, "critical"),
    ("nohup sudo timeout 5 rm -rf /var/x", {"privilege", "delete"}, {"system_path"}, "critical"),
    ("env FOO=1 sudo -E chmod 777 /etc/shadow", {"privilege", "perm_change"}, {"system_path"}, "critical"),
    ("su -c 'reboot'", {"privilege", "process_control"}, set(), "critical"),
    ("sudo apt install -y nginx", {"privilege", "network"}, set(), "dangerous"),
    # everyday safe / caution commands
    ("ls -la", {"read"}, set(), "safe"),
    ("cat a.txt | grep -i err | wc -l", {"read"}, set(), "safe"),
    ("sed 's/a/b/' f.txt", {"read"}, set(), "safe"),
    ("sed -i.bak 's/a/b/' f.txt", {"overwrite"}, set(), "caution"),
    ("git status", {"read"}, set(), "safe"),
    ("git clean -fdx", {"delete"}, set(), "caution"),
    ("crontab -l", {"read"}, set(), "safe"),
    ("tar -xzf release.tar.gz -C /usr/local", {"write"}, {"system_path"}, "critical"),
    ("mkdir -p ./old && mv ./build ./old/", {"write"}, {"single"}, "caution"),
    ("kill -9 1234", {"process_control"}, set(), "dangerous"),
    (":(){ :|:& };:", {"process_control"}, set(), "critical"),
]


@pytest.mark.parametrize("cmd,effects,scopes,risk", CASES, ids=[c[0] for c in CASES])
def test_rule_table(cmd, effects, scopes, risk):
    a = analyze(cmd)
    assert a.parseable, a.reasons
    assert effects <= a.effects, (a.effects, a.reasons)
    assert scopes <= a.scopes, a.scopes
    assert a.risk == risk, [(x.tool, x.effect, sorted(x.scopes), x.risk) for x in a.actions]


@pytest.mark.parametrize("cmd", [
    "", "   ", "rm -rf \"unterminated", ")", "if then fi", "bash -c 'rm \"oops'",
])
def test_unparseable_fails_closed(cmd):
    a = analyze(cmd)
    assert not a.parseable
    assert rank(a.risk) >= rank("dangerous")


@pytest.mark.parametrize("cmd", ["frobnicate x", "python3 cleanup.py", "bash deploy.sh",
                                 "echo 'rm -rf /' | sh", "eval \"$CMD\"", "source ./env.sh"])
def test_unknown_or_opaque_code_is_at_least_dangerous(cmd):
    assert rank(analyze(cmd).risk) >= rank("dangerous")


def test_adding_a_command_never_lowers_risk():
    cmds = [c[0] for c in CASES]
    for a in cmds:
        for b in cmds[::5]:
            combined = analyze(f"{a}; {b}").risk
            assert rank(combined) >= max(rank(analyze(a).risk), rank(analyze(b).risk)), (a, b)


def test_levels_are_ordered():
    assert LEVELS == ["safe", "caution", "dangerous", "critical"]


def test_local_backend_refuses_without_unshare(monkeypatch):
    from vishell.sandbox import backends
    monkeypatch.setattr(backends.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="unsandboxed"):
        backends.LocalBackend()


def test_opaque_flag_marks_incomplete_effects():
    assert analyze("python3 gen.py > out.json").opaque
    assert analyze("eval \"$X\"").opaque
    assert not analyze("rm -rf ./build").opaque
    assert analyze("env").parseable and analyze("env").risk == "safe"
