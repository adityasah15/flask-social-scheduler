# app.py (final, Render-ready)

from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from datetime import datetime, timedelta, timezone
import os
from werkzeug.utils import secure_filename
import logging

# ---------- Setup ----------
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///posts.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
socketio = SocketIO(app)

# IST timezone for aware datetimes
IST = timezone(timedelta(hours=5, minutes=30))

# ---------- Database Model ----------
class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="scheduled")  # Changed "pending" to "scheduled" for consistency
    image_filename = db.Column(db.String(300), nullable=True)

# ---------- Scheduler (persistent job store) ----------
jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///jobs.db')}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone=IST)
scheduler.start()

def _job_id(post_id: int) -> str:
    """Generates a consistent job ID for a post."""
    return f"post_{post_id}"

# ---------- Scheduler Job Function ----------
def publish_post(post_id: int):
    """Job invoked by APScheduler to 'publish' a post."""
    with app.app_context():
        post = Post.query.get(post_id)
        if not post:
            logging.warning(f"[publish_post] Post with id={post_id} not found.")
            return
        if post.status != "scheduled":
            logging.info(f"[publish_post] Post id={post_id} is not in 'scheduled' state (current: {post.status}). Skipping.")
            return
        
        # Mark as posted
        post.status = "posted"
        db.session.commit()
        
        # Emit socket event to notify connected clients
        socketio.emit("status_update", {"post_id": post.id, "status": "posted"})
        logging.info(f"📢 [publish_post] Post id={post_id} marked as 'posted' at {datetime.now(IST)}")

# ---------- Scheduler Setup (run once at startup) ----------
with app.app_context():
    db.create_all()
    scheduled_posts = Post.query.filter_by(status="scheduled").all()
    logging.info(f"Found {len(scheduled_posts)} scheduled posts to process at startup.")
    for post in scheduled_posts:
        run_time = post.scheduled_time
        # Ensure datetime is timezone-aware
        if run_time.tzinfo is None:
            run_time = run_time.replace(tzinfo=IST)

        # If scheduled time has passed, run the job immediately
        if run_time <= datetime.now(IST):
            logging.info(f"Post id={post.id} scheduled time is in the past. Publishing immediately.")
            publish_post(post.id)
        else:
            # Schedule the job
            try:
                scheduler.add_job(
                    func=publish_post,
                    trigger='date',
                    run_date=run_time,
                    args=[post.id],
                    id=_job_id(post.id),
                    replace_existing=True
                )
                logging.info(f"Scheduled job for post id={post.id} at {run_time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as e:
                logging.error(f"[SchedulerSetup] Failed to add job for post {post.id}: {e}")

# ---------- Routes ----------
@app.route('/')
def index():
    posts = Post.query.order_by(Post.scheduled_time.desc()).all()
    return render_template("index.html", posts=posts)

@app.route('/schedule', methods=['GET', 'POST'])
def schedule():
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        platform = request.form['platform']
        
        # Parse datetime from form and make it timezone-aware
        scheduled_time_str = request.form['scheduled_time']
        scheduled_time = datetime.strptime(scheduled_time_str, "%Y-%m-%dT%H:%M").replace(tzinfo=IST)

        # Handle image upload
        image_filename = None
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file.filename != '':
                image_filename = secure_filename(image_file.filename)
                image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], image_filename))

        # Save to DB
        new_post = Post(
            title=title, content=content, platform=platform,
            scheduled_time=scheduled_time, image_filename=image_filename
        )
        db.session.add(new_post)
        db.session.commit()

        # Schedule the job
        try:
            scheduler.add_job(
                func=publish_post, trigger='date', run_date=scheduled_time,
                args=[new_post.id], id=_job_id(new_post.id), replace_existing=True
            )
            logging.info(f"[Schedule] Successfully scheduled job for new post id={new_post.id}")
        except Exception as e:
            logging.error(f"[Schedule] Failed to schedule job for new post {new_post.id}: {e}")

        return redirect(url_for('index'))
    return render_template("schedule.html")

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit(id):
    post = Post.query.get_or_404(id)
    if request.method == 'POST':
        post.title = request.form['title']
        post.content = request.form['content']
        post.platform = request.form['platform']
        post.scheduled_time = datetime.strptime(request.form['scheduled_time'], "%Y-%m-%dT%H:%M").replace(tzinfo=IST)

        # Handle optional image replacement
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file.filename != '':
                # Optionally remove the old image
                if post.image_filename:
                    old_image_path = os.path.join(app.config['UPLOAD_FOLDER'], post.image_filename)
                    if os.path.exists(old_image_path):
                        os.remove(old_image_path)
                
                new_filename = secure_filename(image_file.filename)
                image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
                post.image_filename = new_filename

        db.session.commit()

        # Reschedule the job
        try:
            scheduler.add_job(
                func=publish_post, trigger='date', run_date=post.scheduled_time,
                args=[post.id], id=_job_id(post.id), replace_existing=True
            )
            logging.info(f"[Edit] Successfully rescheduled job for post id={post.id}")
        except Exception as e:
            logging.error(f"[Edit] Failed to reschedule job for post {post.id}: {e}")

        return redirect(url_for('index'))
    return render_template("edit.html", post=post)

@app.route('/delete/<int:id>')
def delete(id):
    post = Post.query.get_or_404(id)
    
    # Remove scheduled job if it exists
    job_id = _job_id(post.id)
    try:
        scheduler.remove_job(job_id)
        logging.info(f"[Delete] Removed job for post id={post.id}")
    except Exception:
        logging.warning(f"[Delete] Job for post id={post.id} not found in scheduler, might have already run.")

    # Remove uploaded image file
    if post.image_filename:
        image_path = os.path.join(app.config['UPLOAD_FOLDER'], post.image_filename)
        if os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception as e:
                logging.error(f"[Delete] Failed to remove image file {image_path}: {e}")

    # Delete from DB
    db.session.delete(post)
    db.session.commit()
    
    return redirect(url_for('index'))

# Serve uploaded images
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ---------- Run App ----------
if __name__ == '__main__':
    # Use debug=False to prevent the Flask dev reloader from running the scheduler twice
    port = int(os.environ.get("PORT", 5000))  # ✅ Render provides PORT env var
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
