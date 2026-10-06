# AIDAS-A100 storage panopticon

Hourly storage-usage dashboard for the AIDAS-A100 lab server: **https://yejoon.me/aidas-panopticon/**

`main` holds the source. `gh-pages` holds the generated page and is replaced on every publish.

## How it works
1. **Scan** (root, hourly at :05). `/etc/cron.d/aidas-storage` runs `/usr/local/sbin/aidas-storage-scan`
   (both from `aidas/`). It only reads (`du -x`, `find -xdev`, `repquota`, `statvfs`) and writes totals,
   no file paths, to `/mnt/TrueNAS/yejoon/aidas-storage/latest.json`. Log: `/var/log/aidas-storage-scan.log`.
2. **Publish** (yejoon's crontab on aidas, every 15 min). `publish.py` reads that file. When it is a new scan
   with the expected shape, it keeps one snapshot per KST day for 30 days (`state/history.json`), renders
   `template.html`, and force-pushes one commit (`index.html`, `data.json`) to `gh-pages` with a deploy key
   that can push to this repository only. If GitHub's port 22 is blocked it uses port 443. Errors go to
   `publish.log`, once per distinct error.

## Set up the publisher on aidas (as yejoon, no sudo; needs Python 3.7+ and git)
```sh
git clone https://github.com/yejoon-lee/aidas-panopticon.git ~/aidas-panopticon
ssh-keygen -t ed25519 -N "" -C "aidas-panopticon publisher" -f ~/.ssh/aidas_panopticon_deploy
cat ~/.ssh/aidas_panopticon_deploy.pub
```
Add that key at https://github.com/yejoon-lee/aidas-panopticon/settings/keys/new with **Allow write access**. Then:
```sh
python3 ~/aidas-panopticon/publish.py --force   # first publish; copies the history from the live page
crontab -e                                      # add the line below
```
```
*/15 * * * * /usr/bin/python3 $HOME/aidas-panopticon/publish.py >> $HOME/aidas-panopticon/publish.log 2>&1
```

## Maintain
- Update the page or publisher: `git -C ~/aidas-panopticon pull` (applies at the next scan; `publish.py --force` applies it now).
- Update the scanner: pull, then `sudo bash ~/aidas-panopticon/aidas/install.sh`. `aidas/uninstall.sh` removes it.
- Stop publishing: remove the crontab line and delete the deploy key on GitHub.
- Preview on any machine: save the `latest` part of the live `data.json` as `scan.json`, then
  `python3 publish.py --input scan.json --out /tmp/site --no-push`.

## What is counted
- `/`: per user by file owner (= quota): /home, /tmp, /var/tmp, other; quota limit from `repquota`.
- `/mnt/nvme0-2`: each user's data folder (`/mnt/data/<user>`), the whole folder whoever owns the files.
- `/mnt/nvme3`: per user `.cache`, conda `userenvs`, Singularity cache and owned images; Docker, the conda
  base install and root's files are listed as common.
- Network mounts (/mnt/nas, /mnt/nasdata, /mnt/TrueNAS, /mnt/ece) are not counted.
