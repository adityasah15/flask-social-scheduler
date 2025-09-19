from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.base import ConflictingIdError
from datetime import datetime, timedelta, timezone
import os
from werkzeug.utils import secure_filename

# ---------- Setup ----------
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///posts.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
db = SQLAlchemy(app)
socketio = SocketIO(app)

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

# ---------- Database Model ----------
class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="pending")
    image_filename = db.Column(db.String(300), nullable=True)  # stores image filename

# ---------- Persistent Scheduler ----------
jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///jobs.db')}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()

# ---------- Scheduler Job ----------
def publish_post(post_id):
    with app.app_context():
        post = Post.query.get(post_id)
        if post and post.status == "pending":
            print(f"📢 Posting to {post.platform}: {post.title} at {datetime.now(IST)}")
            post.status = "posted"
            db.session.commit()
            # Notify frontend via socket
            socketio.emit("status_update", {"post_id": post.id, "status": "posted"})

# ---------- Setup pending posts once ----------
@app.before_request
def setup_scheduler_once():
    if not hasattr(app, "_scheduler_setup_done"):
        db.create_all()
        posts = Post.query.filter(Post.status=="pending").all()
        for post in posts:
            run_time = post.scheduled_time
            if run_time.tzinfo is None:
                run_time = run_time.replace(tzinfo=IST)

            job_id = f"post_{post.id}_{int(run_time.timestamp())}"

            if run_time <= datetime.now(IST):
                publish_post(post.id)
            else:
                try:
                    scheduler.add_job(
                        func=publish_post,
                        trigger='date',
                        run_date=run_time,
                        args=[post.id],
                        id=job_id,
                        replace_existing=True
                    )
                except ConflictingIdError:
                    scheduler.remove_job(job_id)
                    scheduler.add_job(
                        func=publish_post,
                        trigger='date',
                        run_date=run_time,
                        args=[post.id],
                        id=job_id
                    )

        app._scheduler_setup_done = True

# ---------- Routes ----------
@app.route('/')
def index():
    posts = Post.query.order_by(Post.scheduled_time).all()
    return render_template("index.html", posts=posts)

@app.route('/schedule', methods=['GET', 'POST'])
def schedule():
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        platform = request.form['platform']
        scheduled_time = datetime.strptime(request.form['scheduled_time'], "%Y-%m-%dT%H:%M").replace(tzinfo=IST)

        # Handle image upload
        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename:
            image_filename = secure_filename(image_file.filename)
            image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], image_filename))

        # Save post
        new_post = Post(title=title, content=content, platform=platform,
                        scheduled_time=scheduled_time, image_filename=image_filename)
        db.session.add(new_post)
        db.session.commit()

        # Schedule job
        job_id = f"post_{new_post.id}_{int(scheduled_time.timestamp())}"
        try:
            scheduler.add_job(
                func=publish_post,
                trigger='date',
                run_date=scheduled_time,
                args=[new_post.id],
                id=job_id,
                replace_existing=True
            )
        except ConflictingIdError:
            scheduler.remove_job(job_id)
            scheduler.add_job(
                func=publish_post,
                trigger='date',
                run_date=scheduled_time,
                args=[new_post.id],
                id=job_id
            )

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

        # Handle image replacement
        image_file = request.files.get('image')
        if image_file and image_file.filename:
            image_filename = secure_filename(image_file.filename)
            image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], image_filename))
            post.image_filename = image_filename

        db.session.commit()
        return redirect(url_for('index'))
    return render_template("edit.html", post=post)

@app.route('/delete/<int:id>')
def delete(id):
    post = Post.query.get_or_404(id)
    if post.image_filename:
        image_path = os.path.join(app.config['UPLOAD_FOLDER'], post.image_filename)
        if os.path.exists(image_path):
            os.remove(image_path)
    db.session.delete(post)
    db.session.commit()
    return redirect(url_for('index'))

# Serve uploaded images
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ---------- Run App ----------
if __name__ == '__main__':
    socketio.run(app, debug=True, host="0.0.0.0")
