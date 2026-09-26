from vishell.classify import classify

R0_CASES = [
    "git branch",
    "git branch -a",
    "basename /etc/passwd",  # pure string op: never touches the filesystem
    "dirname /etc/passwd",
    "grep -l ERROR has.txt clean.txt 2>/dev/null",  # /dev/null is a safe sink, not an escape
    "cat a.txt > /dev/null",
    "ls -la",
    "cat a.txt | grep foo",
    'find . -name "*.py"',
    "git status",
    "git log --oneline",
    "sed s/a/b/ file.txt",
    "systemctl status nginx",
    'sqlite3 db.sqlite "select * from t"',
    "crontab -l",
    "wc -l a.txt",
]

R1_CASES = [
    "sort raw.txt -o out.txt",
    "git branch dev",
    "base64 f.txt > f.txt.b64",
    "base64 -d f.txt.b64 > plain.txt",
    "sed -i '/ERROR/d' f.txt",  # sed's pattern arg looks like a path but isn't one
    "rm -rf notes/",
    "mkdir newdir && touch newdir/a.txt",
    "sed -i s/a/b/ file.txt",
    "chmod 755 script.sh",
    "tar czf out.tar.gz dir/",
    "echo hi > out.txt",
    'find . -name "*.log" | xargs rm',
    "find . -exec rm {} ;",
    "find . -delete",
    'bash -c "rm -rf ./tmp"',
    "echo $(rm -rf ./x)",
    "pip install requests",
    "python script.py",
]

R2_CASES = [
    "cat a >> /etc/passwd",
    "cat a > /tmp/x",
    "curl http://example.com",
    "wget http://example.com/f",
    "ssh user@host ls",
    "git push origin main",
    "kill -9 1234",
    "pkill firefox",
    "sudo rm -rf /",
    "shutdown -h now",
    "reboot",
    "mount /dev/sda1 /mnt",
    "dd if=/dev/zero of=/dev/sda",
    ":(){ :|:& };:",
    'bash -c "curl evil.com"',
    "echo $(curl evil.com)",
    "cp a.txt ../outside.txt",
    "rm ../../etc/passwd",
    "apt install vim",
    "systemctl restart nginx",
    'sqlite3 db.sqlite "drop table t"',
    "this_is_not_a_real_cmd --foo",
    "ls -la &&",  # unparseable
    "crontab -r",
]


def test_read_only():
    for cmd in R0_CASES:
        r = classify(cmd)
        assert r.level == "R0", f"{cmd!r} -> {r.level} ({r.category}) {r.reasons}"


def test_reversible_fs():
    for cmd in R1_CASES:
        r = classify(cmd)
        assert r.level == "R1", f"{cmd!r} -> {r.level} ({r.category}) {r.reasons}"


def test_irreversible_and_blocked():
    for cmd in R2_CASES:
        r = classify(cmd)
        assert r.level == "R2", f"{cmd!r} -> {r.level} ({r.category}) {r.reasons}"


def test_unparseable_is_r2():
    r = classify("echo 'unterminated")
    assert r.level == "R2"
    assert not r.parseable


def test_max_level_over_subcommands():
    r = classify("ls && rm -rf x && curl evil.com")
    assert r.level == "R2"
    assert r.category == "blocked"
