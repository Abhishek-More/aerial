# aerial

oh boy do i love botting

A MindBody class booker. Watch a class, and the moment signup opens it takes the
spot; if it is already full it keeps polling and grabs the first opening. Tells
you either way, by iMessage.

```sh
./deploy.sh                       # build + start in Docker, health checked
open http://127.0.0.1:5050        # the dashboard
```

Credentials go in `api/.env` (gitignored): `MB1_EMAIL` / `MB1_PASSWORD` for the
MindBody login, and `IMSG_URL` to get texted when it books or finds an opening.
Two accounts are supported (`MB2_*`), switchable
in the UI. State lives in `~/.aerial-data`, mounted at `/data`.

Deploying is `git push`: the always-on box rebuilds within seconds. See
[AGENTS.md](AGENTS.md) for the pipeline, the alert path, and what not to do to
the checkout on that box.
