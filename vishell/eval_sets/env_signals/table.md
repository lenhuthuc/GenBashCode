| signal | n | (b) path | (c) path+env, snapshot | (c) path+env, no snapshot |
|---|---|---|---|---|
| secret | 9 | 1/9 | 9/9 | 9/9 |
| secret-miss | 3 | 0/3 | 0/3 | 0/3 |
| perm | 7 | 1/7 | 7/7 | 7/7 |
| git | 8 | 1/8 | 1/8 | 8/8 |
| bulk | 3 | 0/3 | 2/3 | 2/3 |
| secret+git | 1 | 0/1 | 1/1 | 1/1 |
| secret+perm | 1 | 0/1 | 1/1 | 1/1 |
| perm+git | 1 | 0/1 | 1/1 | 1/1 |
| benign | 12 | 2/12 | 0/12 | 2/12 |
| benign-ignored | 2 | 1/2 | 1/2 | 2/2 |
| **important (all)** | 33 | 3/33 | 22/33 | 29/33 |
| **benign: false alarms** | 14 | 3/14 | 1/14 | 4/14 |

Caught by (c) with snapshot but missed by (b): 19

- `rm psswrd` (secret): block — secret in psswrd; workspace snapshot: in-workspace changes are undoable
- `echo test > q7x` (secret): block — secret in q7x; workspace snapshot: in-workspace changes are undoable
- `rm .k` (secret): block — secret in .k; workspace snapshot: in-workspace changes are undoable
- `truncate -s 0 zz` (secret): block — secret in zz; workspace snapshot: in-workspace changes are undoable
- `sed -i 's/ghp/x/' n2` (secret): block — secret in n2; workspace snapshot: in-workspace changes are undoable
- `cp README.md a.dat` (secret): block — secret in a.dat; workspace snapshot: in-workspace changes are undoable
- `rm tmp3` (secret): block — secret in tmp3; workspace snapshot: in-workspace changes are undoable
- `chmod 777 f_` (secret): block — secret in f_; workspace snapshot: in-workspace changes are undoable
- `rm m1` (perm): confirm — owner/mode/permission: m1; workspace snapshot: in-workspace changes are undoable
- `echo x > m2` (perm): confirm — owner/mode/permission: m2; workspace snapshot: in-workspace changes are undoable
- `sed -i 's/notes/x/' m3` (perm): confirm — owner/mode/permission: m3; workspace snapshot: in-workspace changes are undoable
- `rm -f m4` (perm): confirm — owner/mode/permission: m4; workspace snapshot: in-workspace changes are undoable
- `truncate -s 0 m5` (perm): confirm — owner/mode/permission: m5; workspace snapshot: in-workspace changes are undoable
- `cp README.md m6` (perm): confirm — owner/mode/permission: m6; workspace snapshot: in-workspace changes are undoable
- `git checkout -- .` (bulk): confirm — workspace snapshot: in-workspace changes are undoable; dry run changed 40 files (> 20)
- `tar xf backup.tar` (bulk): confirm — workspace snapshot: in-workspace changes are undoable; dry run changed 40 files (> 20)
- `rm e2` (secret+git): block — secret in e2; not tracked by git: e2; workspace snapshot: in-workspace changes are undoable
- `rm id_q` (secret+perm): block — secret in id_q; not tracked by git: id_q; owner/mode/permission: id_q; workspace snapshot: in-workspace changes are undoable
- `rm v` (perm+git): confirm — not tracked by git: v; owner/mode/permission: v; workspace snapshot: in-workspace changes are undoable
