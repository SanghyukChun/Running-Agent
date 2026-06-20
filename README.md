# Running Agent

## Strava API Setting

1. Sing in to Strava, and go to https://www.strava.com/settings/api
2. Make a new application
   - Authorization Callback Domain: `localhost` (no ULR or port)
4. Check `Client ID` and `Client Secret`

## Initial Auth

```bash
$ python3 scripts/strava_sync.py auth

# Enter your information
Strava client_id: YOUR_CLIENT_ID
Strava client_secret: YOUR_CLIENT_SECRET
```

Tokens will be stored at `.strava-sync/credentials.json`

## Sync your records

```
$ python3 scripts/strava_sync.py auth --client-secret YOUR_CLIENT_SECRET
```
