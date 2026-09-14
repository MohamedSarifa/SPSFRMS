# SPSFRMS - Public Safety Feedback & Response Management System

A beginner-friendly Flask mini project for public complaint submission, department routing, officer status updates and citizen tracking.

## Technology
- Python Flask
- Flask-SQLAlchemy
- PostgreSQL
- HTML, CSS, JavaScript
- Bootstrap 5
- Gunicorn for Render

## 1. Local PostgreSQL setup

Install PostgreSQL and create a database named:

CREATE DATABASE spsfrms_db;

Copy `.env.example` to `.env` and set:

DATABASE_URL=postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5432/spsfrms_db
SECRET_KEY=your-local-secret

Do not upload `.env` to GitHub.

## 2. Install packages

Windows:

python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt

## 3. Run

python app.py

Open:
http://127.0.0.1:5000/

The application automatically creates tables and the basic departments when it starts.

## Demo accounts

Officer:
officer@spsfrms.com
Password: officer123

Administrator:
admin@spsfrms.com
Password: admin123

Citizens can register normally from the website.

## 4. Render deployment

Push the project to GitHub.

Option A - easiest:
1. Create a PostgreSQL database in Render.
2. Create a Python Web Service from the GitHub repository.
3. Build Command:
   pip install -r requirements.txt
4. Start Command:
   gunicorn app:app
5. Add environment variable:
   DATABASE_URL = your Render PostgreSQL INTERNAL database URL
6. Add:
   SECRET_KEY = a secure random value (Render can generate one).

Option B:
The included `render.yaml` can create a web service and a PostgreSQL database together. Review the generated resource names in Render before deployment.

## Important Render note

Render provides an internal database URL for services hosted in the same region. Use that URL for the deployed Flask service. The external URL is for connections from outside Render, such as your local computer.

Uploaded images are stored in the local `uploads` folder. Render's default web filesystem is not persistent across deploys/restarts, so this mini project treats image upload as optional. If permanent image storage is required later, add a dedicated object-storage service.

## Health check

Open `/health` after deployment. A successful response reports that the database is connected.

## Project flow

Citizen -> Register/Login -> Submit Complaint -> Basic Keyword Check -> Department -> Officer Review -> Status Update -> Citizen Tracking

The keyword check uses predefined words only. It does not use an AI model.
