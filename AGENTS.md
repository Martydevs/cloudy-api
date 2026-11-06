# Project overview
UV init starter python template. This has been modified to be a FastAPI web service.

This FastAPI webservice is part of the Cloudy app product.

Cloudy app is a webapp to automating and improving workflows for a christian multimedia workteam for producing and managing media files.

# Running project
Command: `bun run dev`

### Notes
This project is deployed on Vercel through the Container Runtime (beta): the root `Dockerfile` is built by Vercel (`vercel.json` sets `"framework": "container"`) and runs as a Vercel Function. It downloads videos on demand for a Nuxt Fullstack Webapp, also hosted on Vercel. `PORT=8000` must be set in the Vercel project settings. See README "Deploying to Vercel" for the platform constraints (stateless instances, 4.5 MB body limit, duration limits).