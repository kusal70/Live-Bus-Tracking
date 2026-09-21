# Live Bus Tracking

Desktop live bus tracking system built with Python and Tkinter.

## Features
- Developer, driver, and student roles
- Live driver location sharing through Supabase
- Student map showing active buses
- Driver live GPS tracking on Windows
- Local SQLite logging and CSV export
- Driver-location focus buttons

## Setup

1. Install Python 3.10+.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and configure your Supabase project.
4. Create the required Supabase `bus_locations` table with columns:
   - `driver` text
   - `lat` double precision
   - `lng` double precision
   - `updated_at` timestamptz
5. Run the application with:
   `python bussssss.py`

## Security
Do not commit real Supabase keys or administrator passwords. Keep secrets in environment variables.
