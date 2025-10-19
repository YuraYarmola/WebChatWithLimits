# Priority Streams Chat (MVP) — with HTML Client & Admin UI

## Run
```bash
cp .env.example .env
docker compose up -d
# Open
#  - Client: http://localhost:8000/
#  - Admin:  http://localhost:8000/admin-ui
```

## Demo users & channel
DEV_MODE=true seeds:
- Users: 1 (Alice), 2 (Bob)
- Channel: 1 (General), participants: 1, 2

## WebSocket
ws://localhost:8000/ws?user_id=1
