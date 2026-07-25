# Poulse Kora Backend

## Features

- FastAPI
- React Admin
- SQLAlchemy and Alembic
- Pre-commit hooks (black, autoflake, isort, flake8, prettier)
- Github Action
- Dependabot config
- Docker images

## Good to know

The frontend of this project uses React Admin. Follow the quick tutorial to understand how [React Admin](https://marmelab.com/react-admin/Tutorial.html) works.

## Step 1: Getting started

Start a local development instance with docker compose

```bash
docker compose up -d

# Run database migration
docker compose exec backend alembic upgrade head

# Create database used for testing
docker compose exec postgres createdb apptest -U postgres
```

Now you can navigate to the following URLs:

- Backend OpenAPI docs: http://localhost:8000/docs/
- Frontend: http://localhost:3000

### Step 2: Setup pre-commit hooks and database

Keep your code clean by using the configured pre-commit hooks. Follow the [instructions here to install pre-commit](https://pre-commit.com/). Once pre-commit is installed, run this command to install the hooks into your git repository:

```bash
pre-commit install
```

### Local development

The backend setup of docker compose is set to automatically reload the app whenever code is updated. However, for frontend it's easier to develop locally.

```bash
docker compose stop frontend
cd frontend
yarn
yarn start
```

If you want to develop against something other than the default host, localhost:8000, you can set the `REACT_APP_API_BASE` environment variable:

```bash
export REACT_APP_API_BASE=http://mydomain.name:8000
yarn start
```

Don't forget to edit the `.env` file and update the `BACKEND_CORS_ORIGINS` value (add `http://mydomain:3000` to the allowed origins).

### Rebuilding containers

If you add a dependency, you'll need to rebuild your containers like this:

```bash
docker compose up -d --build
```

### Resetting the local environment

To wipe Postgres and Redis completely and start from a clean slate (e.g. local state got
into a bad shape), tear the containers down along with their volumes and bring everything
back up:

```bash
docker compose down -v
docker compose up -d
docker compose exec backend alembic upgrade head
docker compose exec postgres createdb apptest -U postgres
```

`down -v` removes the `app-db-data` and `redis-data` volumes, so both come back empty.
Re-run `seed_dev_data.py` afterwards (see below) if you want dev data back.

### Regenerate front-end API package

Instead of writing frontend API client manually, OpenAPI Generator is used. Typescript bindings for the backend API can be recreated with this command:

```bash
yarn genapi
```

### Database migrations

These two are the most used commands when working with alembic. For more info, follow through [Alembic's tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html).

```bash
# Auto generate a revision
docker compose exec backend alembic revision --autogenerate -m 'message'

# Apply latest changes
docker compose exec backend alembic upgrade head
```

### Backend tests

The `Backend` service uses a hardcoded database named `apptest`. First, ensure that it's created

```bash
docker compose exec postgres createdb apptest -U postgres
```

Then you can run tests with this command:

```bash
docker compose run backend pytest --cov --cov-report term-missing
```

### Seed dev data

`seed_dev_data.py` creates a handful of fixed "bot" users and tops up every channel with a
few realistic posts from them, then reconciles the Redis feed state (channel subscriber
sets, free-queue, token balances, and per-user review queues) from Postgres. This gives a
real dev/mobile-app account something to review as soon as it subscribes to a channel via
the app — `get_posts_feed` excludes posts authored by the viewer, so a single real account
would otherwise never see anything in its own feed.

```bash
docker compose exec backend python seed_dev_data.py
```

Safe to re-run: bot users are matched by email, and each channel is only topped up to a
fixed number of bot posts, so re-running won't pile up duplicates.

If Redis distribution state ever drifts from Postgres on its own (e.g. after manually
flushing Redis, or restoring a Postgres backup without matching Redis data), reconcile it
directly without creating any new posts:

```bash
docker compose exec backend python rebuild_redis.py
```

### Single docker image

There's a monolith/single docker image that uses FastAPI to serve static assets. You can use this image to deploy directly to Heroku, Fly.io or anywhere where you can run a Dockerfile without having to build a complicated setup out of separate frontend and backend images.

## Recipes

#### Build and upload docker images to a repository

Configure the [**build-push-action**](https://github.com/marketplace/actions/build-and-push-docker-images) in `.github/workflows/test.yaml`.

## Credits

Created with [FastAPI Starter](https://github.com/gaganpreet/fastapi-starter)
