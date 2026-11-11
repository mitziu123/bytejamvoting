import os
import random
import string
from datetime import datetime
from functools import wraps
from ipaddress import ip_address, ip_network

from flask import (
    Flask, render_template, request, redirect, jsonify,
    url_for, flash, session, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import BadRequest

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import desc
from sqlalchemy.sql import func

# ---------- App / Config ----------
app = Flask(__name__)

# Use environment secret if available; fallback to a random secret for dev (not for prod)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

# DB config: DATABASE_URL env var overrides the default Render database below
_db_url = os.environ.get(
    'DATABASE_URL',
    'postgresql://movement_ulas_user:9Kh8ZtfvYtMzICBvWcpJRwlZhUSUkGMJ@dpg-cpa93dm3e1ms739m73fg-a.oregon-postgres.render.com/movement_ulas'
)
# Normalize to the psycopg2 driver (SQLAlchemy 2.1 defaults postgresql:// to psycopg 3,
# and no longer accepts the legacy "postgres://" scheme)
for _scheme in ('postgres://', 'postgresql://'):
    if _db_url.startswith(_scheme):
        _db_url = 'postgresql+psycopg2://' + _db_url[len(_scheme):]
        break
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Admin credentials: pull password from env if possible (safer than hardcoding)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "ihccadminjslj")
_admin_password_plain = os.environ.get("ADMIN_PASSWORD", "jslj!1238!")  # replace in prod
_ADMIN_PASSWORD_HASH = generate_password_hash(_admin_password_plain)

# Accept exact addresses and CIDR ranges. Render supplies RENDER automatically.
ALLOW_ALL = os.environ.get("ALLOW_ALL", "true").lower() in ("1", "true", "yes")
ON_RENDER = os.environ.get("RENDER") == "true"
ALLOWED_NETWORKS = tuple(
    ip_network(entry.strip())
    for entry in os.environ.get("ALLOWED_CONN", "").split(",")
    if entry.strip()
) if not ALLOW_ALL else ()

db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.Enum('student', 'guest', 'coach', 'company', name='role_enum'), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    pin = db.Column(db.String(4), nullable=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    ip_address = db.Column(db.String(50), nullable=False)

    company = db.relationship('Company', backref='users', lazy=True)
    team = db.relationship('Team', backref='team_members', lazy=True)

    def __init__(self, role, first_name, last_name, pin=None, company_id=None, team_id=None, ip_address=None):
        self.role = role
        self.first_name = first_name
        self.last_name = last_name
        self.pin = pin
        self.company_id = company_id
        self.team_id = team_id
        self.ip_address = ip_address


class Team(db.Model):
    __tablename__ = 'teams'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    division = db.Column(db.Enum('Bits', 'Bytes', 'IHCC', name='division_enum'), nullable=False)

    def __repr__(self):
        return f'<Team {self.name}>'


class Company(db.Model):
    __tablename__ = 'company'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    pin = db.Column(db.String(4), nullable=False)

    def __repr__(self):
        return f'<Company {self.name}>'


class Vote(db.Model):
    __tablename__ = 'votes'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    category = db.Column(db.String(50), nullable=True)
    score = db.Column(db.Integer, nullable=True)

    team = db.relationship('Team', backref='votes', lazy=True)
    user = db.relationship('User', backref='votes', lazy=True)

    __table_args__ = (db.UniqueConstraint('user_id', 'team_id', 'category', name='unique_user_vote'),)

    def __repr__(self):
        return f'<Vote User {self.user_id} - Team {self.team_id}: {self.category} - {self.score}>'


class VotingSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    voting_start = db.Column(db.DateTime, nullable=False)
    voting_end = db.Column(db.DateTime, nullable=False)


# Create tables at import time so it also runs under gunicorn (not just `python app.py`)
with app.app_context():
    db.create_all()


# ---------- Helpers ----------
STUDENT_CRITERIA = (
    ('project_wow', 'Project Wow!', "Did the team's project amaze you?"),
    ('theme', 'Theme', 'Did the project represent the theme well?'),
    ('performance', 'Performance', 'Did the project perform as intended?'),
)
JUDGING_CRITERIA = (
    ('creativity', 'Creativity', ''),
    ('visualdesign', 'Visual Design', ''),
    ('professionalism', 'Professionalism', ''),
    ('communication', 'Communication', ''),
    ('performance', 'Performance', ''),
)


def voting_divisions(user):
    if user.role == 'student':
        return ('Bits', 'Bytes', 'IHCC')
    if user.role == 'company':
        return ('IHCC',)
    if user.role == 'coach' and user.team:
        return {'Bits': ('Bytes',), 'Bytes': ('Bits',)}.get(user.team.division, ())
    return ()


def rank_teams(rows):
    if not rows:
        return []

    baseline = sum(row.total_score for row in rows) / sum(row.vote_count for row in rows)
    ranked = [
        {
            'team_id': row.team_id,
            'team_name': row.team_name,
            'division': row.division,
            'raw_average': row.total_score / row.vote_count,
            'adjusted_score': (row.total_score + 10 * baseline) / (row.vote_count + 10),
            'vote_count': row.vote_count,
        }
        for row in rows
    ]
    return sorted(ranked, key=lambda team: (-team['adjusted_score'], team['team_name']))


def current_voting_window():
    settings = VotingSettings.query.order_by(desc(VotingSettings.id)).first()
    if settings is None:
        return settings, 'Not scheduled'
    now = datetime.now()
    if now < settings.voting_start:
        return settings, 'Scheduled'
    if now > settings.voting_end:
        return settings, 'Closed'
    return settings, 'Open'


def generate_pin(length: int = 4) -> str:
    return ''.join(random.choices(string.digits, k=length))


def _verify_admin(username: str, password: str) -> bool:
    return username == ADMIN_USERNAME and check_password_hash(_ADMIN_PASSWORD_HASH, password)


def get_client_ip():
    if ON_RENDER:
        # Render's public edge overwrites this header with the connecting IP.
        return request.headers.get('CF-Connecting-IP', '').strip()
    return request.remote_addr or ''


@app.before_request
def limit_remote_addr():
    if ALLOW_ALL:
        return
    try:
        client_ip = ip_address(get_client_ip())
    except ValueError:
        abort(403, description="Connect to the school's Wi-Fi to access ByteJam Voting.")
    if not any(client_ip in network for network in ALLOWED_NETWORKS):
        abort(403, description="Connect to the school's Wi-Fi to access ByteJam Voting.")


# ---------- Auth decorators ----------
def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Admin login required.", "warning")
            return redirect(url_for("admin_login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapper


# ---------- Routes ----------
@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    # allow login page even if voting window closed
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "") or ""
        if _verify_admin(username, password):
            session["is_admin"] = True
            flash("Welcome, admin.", "success")
            dest = request.args.get("next") or url_for("admin_panel")
            return redirect(dest)
        else:
            flash("Invalid admin credentials.", "danger")
    return render_template("admin_login.html")


@app.route("/admin-logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out.", "info")
    return redirect(url_for("admin_login"))


@app.route('/admin')
@admin_required
def admin_panel():
    companies = Company.query.all()

    company_data = {
        c.name: {
            "pin": c.pin,
            # keep company members as mappings too (so admin can see pins)
            "members": [
                {
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "pin": u.pin,
                    "role": u.role
                } for u in c.users
            ]
        }
        for c in companies
    }

    teams = Team.query.all()

    ballots = (
        db.session.query(
            Vote.team_id,
            Vote.user_id,
            User.role,
            func.sum(Vote.score).label('score'),
        )
        .join(User, User.id == Vote.user_id)
        .filter(
            ((User.role == 'student') & Vote.category.in_([label for _, label, _ in STUDENT_CRITERIA]))
            | (User.role.in_(('coach', 'company')) & Vote.category.in_([label for _, label, _ in JUDGING_CRITERIA]))
        )
        .group_by(Vote.team_id, Vote.user_id, User.role)
        .having(
            ((User.role == 'student') & (func.count(Vote.score) == len(STUDENT_CRITERIA)))
            | (User.role.in_(('coach', 'company')) & (func.count(Vote.score) == len(JUDGING_CRITERIA)))
        )
        .subquery()
    )
    totals = (
        db.session.query(
            Team.id.label('team_id'),
            Team.name.label('team_name'),
            Team.division,
            func.sum(ballots.c.score).label('total_score'),
            func.count().label('vote_count'),
        )
        .join(ballots, ballots.c.team_id == Team.id)
        .group_by(Team.id, Team.name, Team.division)
    )
    judging = totals.filter(ballots.c.role.in_(('coach', 'company'))).all()
    award_results = {
        division: rank_teams([row for row in judging if row.division == division])
        for division in ('Bits', 'Bytes', 'IHCC')
    }
    award_results['Student Choice'] = rank_teams(totals.filter(ballots.c.role == 'student').all())

    team_data = {}
    for t in teams:
        division = t.division
        if division not in team_data:
            team_data[division] = {}
        # return member objects (dicts) so template can read pin, role, names
        members = User.query.filter_by(team_id=t.id).all()
        team_data[division][t.name] = {
            "division": t.division,
            "members": [
                {
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "pin": u.pin,
                    "role": u.role
                } for u in members
            ]
        }

    voting_settings, voting_status = current_voting_window()
    return render_template(
        'admin.html',
        companies=company_data,
        teams=team_data,
        award_results=award_results,
        voting_settings=voting_settings,
        voting_status=voting_status,
        voting_timezone=os.environ.get('TZ') or datetime.now().astimezone().tzname(),
    )



@app.route("/create")
def create():
    return render_template("index.html", fresh_pin=generate_pin())


@app.route('/save_voting_settings', methods=['POST'])
@admin_required
def save_voting_settings():
    voting_start = request.form.get('voting_start')
    voting_end = request.form.get('voting_end')

    if not (voting_start and voting_end):
        flash("Both start and end times are required.", "danger")
        return redirect(url_for('admin_panel'))

    try:
        start_dt = datetime.strptime(voting_start, '%Y-%m-%dT%H:%M')
        end_dt = datetime.strptime(voting_end, '%Y-%m-%dT%H:%M')
    except ValueError:
        flash("Invalid date format. Use the date-time picker.", "danger")
        return redirect(url_for('admin_panel'))

    if start_dt >= end_dt:
        flash("Voting start must be before voting end.", "danger")
        return redirect(url_for('admin_panel'))

    new_settings = VotingSettings(voting_start=start_dt, voting_end=end_dt)
    db.session.add(new_settings)
    db.session.commit()

    flash("Voting window saved.", "success")
    return redirect(url_for('admin_panel'))


@app.route('/submit_vote', methods=['POST'])
def submit_vote():
    _, voting_status = current_voting_window()
    if voting_status != 'Open':
        return redirect(url_for('not_allowed'))

    user = db.session.get(User, session.get('user_id')) if 'user_id' in session else None
    if user is None or user.role == 'guest':
        flash("Sign in as a student, coach, or company to vote.", "danger")
        return redirect(url_for('create'))

    team_name = (request.form.get('team') or "").strip()
    team = Team.query.filter_by(name=team_name).first()
    if team is None or team.division not in voting_divisions(user):
        flash("That team is unavailable for your ballot.", "danger")
        return redirect(url_for('dashboard'))

    criteria = STUDENT_CRITERIA if user.role == 'student' else JUDGING_CRITERIA
    try:
        scores = [int(request.form[field]) for field, _, _ in criteria]
        if any(score < 0 or score > (5 if user.role == 'student' else 10) for score in scores):
            raise ValueError
    except (KeyError, ValueError):
        flash("Enter a valid score for every criterion.", "danger")
        return redirect(url_for('dashboard'))

    if Vote.query.filter_by(user_id=user.id, team_id=team.id).first():
        flash("You have already voted for this team.", "danger")
        return redirect(url_for('dashboard'))

    try:
        db.session.add_all([
            Vote(team_id=team.id, user_id=user.id, category=label, score=score)
            for (_, label, _), score in zip(criteria, scores)
        ])
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Error saving votes")
        flash("There was an error saving your vote. Please try again.", "danger")
        return redirect(url_for('dashboard'))

    flash("Your vote has been submitted!", "success")
    return redirect(url_for('dashboard'))


@app.route("/register_user", methods=["POST"])
def register_user():
    user_type = request.form.get('user_type')

    # Get remote IP safely
    ip_address = get_client_ip()

    if user_type == '1':  # Coach or company
        first_name = (request.form.get('m_first_name') or "").strip()
        last_name = (request.form.get('m_last_name') or "").strip()
        company_pin = (request.form.get('company_pin') or "").strip()

        if not (first_name and last_name and company_pin):
            flash("Name and PIN are required.", "danger")
            return redirect(url_for('create'))

        # Try to find a company matching that pin
        company = Company.query.filter_by(pin=company_pin).first()

        user = None
        # 1) If a company exists for that pin, try matching user by company
        if company:
            user = User.query.filter_by(first_name=first_name, last_name=last_name, company_id=company.id, role='company').first()

        # 2) Fallback: match by first/last + pin (covers team members who have pins but no company_id)
        if not user:
            user = User.query.filter_by(first_name=first_name, last_name=last_name, pin=company_pin).filter(User.role.in_(('coach', 'company'))).first()

        if user:
            session['user_id'] = user.id
            session['first_name'] = user.first_name
            session['last_name'] = user.last_name
            session['pin'] = user.pin
            session['role'] = user.role
            flash("Welcome back!", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("User not found. If you should be there, ask an admin to add you.", "danger")
            return redirect(url_for('create'))


    elif user_type == '2':  # Student registration
        try:
            pin = (request.form.get('pin') or "").strip()
            first_name = (request.form.get('first_name') or "").strip()
            last_name = (request.form.get('last_name') or "").strip()
        except BadRequest:
            flash("Invalid form submission.", "danger")
            return redirect(url_for('create'))

        if not (first_name and last_name):
            flash("First and last name are required for student registration.", "danger")
            return redirect(url_for('create'))

        # Basic sanitization: pin should be 4 digits if provided
        if pin and (not pin.isdigit() or len(pin) != 4):
            flash("Invalid PIN format; must be 4 digits.", "danger")
            return redirect(url_for('create'))

        new_user = User(
            role='student',
            first_name=first_name,
            last_name=last_name,
            pin=pin or None,
            ip_address=ip_address or ""
        )

        try:
            db.session.add(new_user)
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Error creating student user")
            flash("Could not create student account. Contact admin.", "danger")
            return redirect(url_for('create'))

        session['user_id'] = new_user.id
        session['first_name'] = new_user.first_name
        session['last_name'] = new_user.last_name
        session['pin'] = new_user.pin
        session['role'] = new_user.role
        flash("Student registered.", "success")
        return redirect(url_for('dashboard'))

    else:
        flash("Invalid user type selected!", "danger")
        return redirect(url_for('create'))


@app.route("/")
def dashboard():
    settings, voting_status = current_voting_window()

    if not settings:
        flash("Voting settings have not been configured yet.", "warning")

    if voting_status != 'Open':
        return redirect(url_for('not_allowed'))

    user = db.session.get(User, session['user_id']) if 'user_id' in session else None
    if user is not None and user.role != 'guest':
        user_id = user.id
        first_name = session.get('first_name')
        last_name = session.get('last_name')
        pin = session.get('pin')

        companies = Company.query.all()
        company_data = {
            c.name: {
                "pin": c.pin,
                "members": [f"{u.first_name} {u.last_name}" for u in c.users]
            }
            for c in companies
        }

        teams = Team.query.filter(Team.division.in_(voting_divisions(user))).all()

        team_data = {}
        for t in teams:
            division = t.division
            if division not in team_data:
                team_data[division] = {}

            user_vote = Vote.query.filter_by(user_id=user_id, team_id=t.id).first()
            has_voted = True if user_vote else False

            team_data[division][t.name] = {
                "division": t.division,
                "members": [f"{u.first_name} {u.last_name}" for u in User.query.filter_by(team_id=t.id).all()],
                "has_voted": has_voted
            }

        return render_template('dashboard.html', first_name=first_name, last_name=last_name, company_pin=pin, companies=company_data, teams=team_data, voter_role=user.role,
                               criteria=STUDENT_CRITERIA if user.role == 'student' else JUDGING_CRITERIA)

    flash('You must be logged in to view the dashboard.', 'warning')
    return redirect('/create')


@app.route('/delete_data', methods=['POST'])
@admin_required
def delete_data():
    try:
        # respect checkboxes; test for keys
        if 'delete_votes' in request.form:
            db.session.query(Vote).delete()

        if 'delete_users' in request.form:
            db.session.query(User).delete()

        if 'delete_teams' in request.form:
            db.session.query(Team).delete()

        if 'delete_companies' in request.form:
            db.session.query(Company).delete()

        db.session.commit()
        flash("Requested data deleted.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Error deleting data")
        flash("There was an error deleting data.", "danger")
    return redirect(url_for('admin_panel'))


@app.route('/not_allowed')
def not_allowed():
    return "Voting is not allowed at this time."


@app.route('/submit', methods=['POST'])
@admin_required
def handle_form():
    action = request.form.get('action')
    entity_type = request.form.get('type')

    try:
        if entity_type == 'company':
            if action == 'add':
                name = (request.form.get('company_name') or "").strip()
                if not name:
                    flash("Company name is required.", "danger")
                    return redirect(url_for('admin_panel'))
                new_company = Company(name=name, pin=generate_pin())
                db.session.add(new_company)
                db.session.commit()
                flash(f"Company '{name}' added successfully!", "success")

            elif action == 'remove':
                company = Company.query.filter_by(name=request.form['company_name']).first()
                if company:
                    db.session.delete(company)
                    db.session.commit()
                    flash(f"Company '{company.name}' removed successfully!", "success")
                else:
                    flash("Company not found!", "danger")

            elif action == 'add_member':
                first_name = (request.form.get('first_name') or "").strip()
                last_name = (request.form.get('last_name') or "").strip()
                pin = (request.form.get('pin') or "").strip()
                company = Company.query.filter_by(name=request.form['company']).first()

                if not (first_name and last_name and pin):
                    flash("Member name and pin are required.", "danger")
                    return redirect(url_for('admin_panel'))

                if company:
                    new_user = User(
                        role='company',
                        first_name=first_name,
                        last_name=last_name,
                        pin=pin,
                        company_id=company.id,
                        ip_address=get_client_ip()
                    )
                    db.session.add(new_user)
                    db.session.commit()
                    flash(f"Member '{first_name} {last_name}' added to '{company.name}'!", "success")
                else:
                    flash("Company not found!", "danger")

            elif action == 'remove_member':
                parts = request.form['member_name'].split()
                if len(parts) < 2:
                    flash("Please provide both first and last name.", "danger")
                    return redirect(url_for('admin_panel'))
                first, last = parts[0], parts[1]
                user = User.query.filter_by(first_name=first, last_name=last).first()
                if user:
                    db.session.delete(user)
                    db.session.commit()
                    flash(f"Member '{user.first_name} {user.last_name}' removed!", "success")
                else:
                    flash("Member not found!", "danger")

        elif entity_type == 'team':
            if action == 'add':
                name = (request.form.get('team_name') or "").strip()
                division = request.form.get('division')
                if not (name and division):
                    flash("Team name and division are required.", "danger")
                    return redirect(url_for('admin_panel'))
                new_team = Team(name=name, division=division)
                db.session.add(new_team)
                db.session.commit()
                flash(f"Team '{name}' added successfully!", "success")

            elif action == 'remove':
                team = Team.query.filter_by(name=request.form['team_name']).first()
                if team:
                    db.session.delete(team)
                    db.session.commit()
                    flash(f"Team '{team.name}' removed successfully!", "success")
                else:
                    flash("Team not found!", "danger")

            elif action in ['add_member', 'add_coach']:
                first_name = (request.form.get('first_name') or "").strip()
                last_name = (request.form.get('last_name') or "").strip()
                role_input = request.form.get('role', 'member')
                team = Team.query.filter_by(name=request.form['team_name']).first()

                if not (first_name and last_name and team):
                    flash("Member name and team are required.", "danger")
                    return redirect(url_for('admin_panel'))

                pin = generate_pin()
                role = 'coach' if role_input.lower() == 'coach' else 'student'
                new_user = User(
                    role=role,
                    first_name=first_name,
                    last_name=last_name,
                    pin=pin,
                    team_id=team.id,
                    ip_address=get_client_ip()
                )
                db.session.add(new_user)
                db.session.commit()
                flash(f"{role.capitalize()} '{first_name} {last_name}' added to '{team.name}'!", "success")

            elif action == 'remove_member':
                parts = request.form['member_name'].split()
                if len(parts) < 2:
                    flash("Please provide both first and last name.", "danger")
                    return redirect(url_for('admin_panel'))
                first, last = parts[0], parts[1]
                team = Team.query.filter_by(name=request.form['team_name']).first()
                if not team:
                    flash("Team not found.", "danger")
                    return redirect(url_for('admin_panel'))

                user = User.query.filter_by(first_name=first, last_name=last, team_id=team.id).first()
                if user:
                    db.session.delete(user)
                    db.session.commit()
                    flash(f"Member '{user.first_name} {user.last_name}' removed from '{request.form['team_name']}'!",
                          "success")
                else:
                    flash("Member not found in the team!", "danger")

    except Exception:
        db.session.rollback()
        app.logger.exception("Error handling admin submit")
        flash("An error occurred processing your request.", "danger")

    return redirect('/admin')


# ---------- Start ----------
if __name__ == '__main__':
    # Turn off debug for production. Use env var FLASK_DEBUG if needed.
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() in ("1", "true", "yes")
    app.run(debug=debug_mode)
