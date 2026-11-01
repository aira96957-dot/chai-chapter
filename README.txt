CHAI & CHAPTER CAFE: website with database
==========================================
No installation of packages is needed. It only uses Python's built-in tools.

START
  Windows : double-click  run.bat
  Mac/Linux: run  ./run.sh   (or: python3 server.py)
Your browser opens at http://localhost:8000
Keep the black window open while using the site. Press Ctrl+C to stop.

ADMIN (see bookings, approve reviews, download CSV)
  http://localhost:8000/admin     username: anything     password: changeme
  Change the password before sharing:
    Windows  : set ADMIN_PASSWORD=mypassword   then run  python server.py
    Mac/Linux: ADMIN_PASSWORD=mypassword ./run.sh

DATABASE
  cafe.db is created on first start. Tables: menu, reservations, reviews.
  Open it with the free "DB Browser for SQLite" to edit menu items and prices.

NOTE: Python itself must be on the computer (python.org). Nothing else is needed.
