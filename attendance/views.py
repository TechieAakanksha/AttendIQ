"""AttendIQ views: dashboards, student/subject management, attendance and face scanning."""
import base64
import binascii
import csv
import json
from datetime import date, datetime, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .face_utils import has_face, match_frame
from .models import (
    Attendance, CourseSession, FaceSample, Profile, ScanEvent,
    Student, Subject, TeachingAssignment,
)

LOW_ATTENDANCE_LIMIT = 75
MIN_FACE_SAMPLES = 3
MAX_FACE_SAMPLES = 12
MAX_SAMPLE_BYTES = 5 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_date(value, default=None):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default=None, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        return default
    return number


def parse_time(value):
    try:
        return datetime.strptime(value or "", "%H:%M").time()
    except ValueError:
        return None


def pct(present, total):
    return round(present * 100 / total, 1) if total else 0


def csv_safe(value):
    """Neutralise spreadsheet formula injection in exported CSV cells."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def current_role(user):
    if not user.is_authenticated:
        return "anonymous"
    if user.is_staff:
        return "teacher"
    try:
        return user.profile.role
    except Profile.DoesNotExist:
        return "student"


def current_student(user):
    try:
        return user.student_record
    except Student.DoesNotExist:
        return None


def scoped_students(user):
    return Student.objects.all() if user.is_staff else Student.objects.filter(teacher=user)


def scoped_subjects(user):
    if user.is_staff:
        return Subject.objects.filter(is_active=True)
    return Subject.objects.filter(is_active=True).filter(Q(teacher=user) | Q(faculty=user)).distinct()


def scoped_assignments(user):
    return TeachingAssignment.objects.filter(teacher=user, is_active=True).select_related("subject")


def teacher_required(view):
    """Login required + teacher/admin role required."""
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if current_role(request.user) != "teacher":
            messages.error(request, "This area is available to teachers and administrators only.")
            return redirect("dashboard")
        return view(request, *args, **kwargs)
    return wrapped


def weekly_trend(att_qs, days=7):
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    rows = (
        att_qs.filter(date__gte=start, date__lte=today)
        .values("date")
        .annotate(total=Count("id"), present=Count("id", filter=Q(status=True)))
        .order_by("date")
    )
    by_date = {row["date"]: row for row in rows}
    labels, values = [], []
    for i in range(days):
        day = start + timedelta(days=i)
        row = by_date.get(day)
        labels.append(day.strftime("%d %b"))
        values.append(pct(row["present"], row["total"]) if row else None)
    return labels, values


def subject_breakdown(att_qs):
    """Per-subject attendance percentages for a set of attendance records."""
    rows = (
        att_qs.filter(session__subject__isnull=False)
        .values("session__subject__code", "session__subject__name")
        .annotate(total=Count("id"), present=Count("id", filter=Q(status=True)))
        .order_by("session__subject__code")
    )
    result = []
    for row in rows:
        percent = pct(row["present"], row["total"])
        result.append({
            "code": row["session__subject__code"],
            "name": row["session__subject__name"],
            "total": row["total"],
            "present": row["present"],
            "percent": percent,
            "low": percent < LOW_ATTENDANCE_LIMIT,
        })
    return result


def get_low_students(limit=None, user=None):
    base = scoped_students(user) if user else Student.objects.all()
    qs = base.annotate(
        total=Count("attendance_records"),
        present=Count("attendance_records", filter=Q(attendance_records__status=True)),
    ).filter(total__gt=0)
    result = [{"student": s, "percent": pct(s.present, s.total)} for s in qs]
    result = [item for item in result if item["percent"] < LOW_ATTENDANCE_LIMIT]
    result.sort(key=lambda item: item["percent"])
    return result[:limit]


def live_session_for(user, subject):
    """Return the teacher's live session for `subject`, creating one when needed."""
    session = CourseSession.objects.filter(teacher=user, status="live").first()
    if session and subject and session.subject_id != subject.id:
        session.status = "ended"
        session.save(update_fields=["status"])
        session = None
    if not session:
        session = CourseSession.objects.create(
            teacher=user,
            subject=subject,
            course_code=subject.code if subject else "CLASS",
            course_name=subject.name if subject else "Class session",
        )
    return session


def check_password_or_message(request, password, user=None):
    """Validate a password with Django's validators; flash the errors when invalid."""
    try:
        validate_password(password, user)
    except ValidationError as exc:
        for error in exc.messages:
            messages.error(request, error)
        return False
    return True


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@login_required
def dashboard(request):
    role = current_role(request.user)

    if role == "student":
        student = current_student(request.user)
        if not student:
            return render(request, "attendance/student_dashboard.html", {"student": None})
        records = Attendance.objects.filter(student=student).select_related("session__subject")
        total = records.count()
        present = records.filter(status=True).count()
        percentage = pct(present, total)
        return render(request, "attendance/student_dashboard.html", {
            "student": student,
            "records": records[:15],
            "total_attendance": total,
            "total_present": present,
            "total_absent": total - present,
            "attendance_percentage": percentage,
            "is_low": total > 0 and percentage < LOW_ATTENDANCE_LIMIT,
            "subject_stats": subject_breakdown(records),
            "limit": LOW_ATTENDANCE_LIMIT,
        })

    students_qs = scoped_students(request.user)
    attendance_qs = Attendance.objects.filter(student__in=students_qs)
    total_present = attendance_qs.filter(status=True).count()
    total_absent = attendance_qs.filter(status=False).count()
    total_attendance = total_present + total_absent
    today_qs = attendance_qs.filter(date=timezone.localdate())
    today_total = today_qs.count()
    dept_rows = (
        attendance_qs.values("student__department")
        .annotate(total=Count("id"), present=Count("id", filter=Q(status=True)))
        .order_by("student__department")
    )
    trend_labels, trend_values = weekly_trend(attendance_qs)
    return render(request, "attendance/dashboard.html", {
        "role": role,
        "total_students": students_qs.count(),
        "enrolled_count": students_qs.filter(face_enrolled=True).count(),
        "total_present": total_present,
        "total_absent": total_absent,
        "attendance_percentage": pct(total_present, total_attendance),
        "today_percentage": pct(today_qs.filter(status=True).count(), today_total),
        "today_total": today_total,
        "recent_attendance": attendance_qs.select_related("student", "session__subject").order_by("-date", "-created_at")[:8],
        "recent_scans": ScanEvent.objects.filter(session__teacher=request.user).select_related("student")[:6],
        "trend_labels": trend_labels,
        "trend_values": trend_values,
        "dept_labels": [row["student__department"] or "Unassigned" for row in dept_rows],
        "dept_values": [pct(row["present"], row["total"]) for row in dept_rows],
        "low_students": get_low_students(5, request.user),
        "limit": LOW_ATTENDANCE_LIMIT,
        "active_session": CourseSession.objects.filter(teacher=request.user, status="live").first(),
    })


# --------------------------------------------------------------------------- #
# Students
# --------------------------------------------------------------------------- #
@teacher_required
def students(request):
    q = request.GET.get("q", "").strip()
    qs = scoped_students(request.user)
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(roll_number__icontains=q)
            | Q(department__icontains=q) | Q(email__icontains=q)
        )
    qs = qs.annotate(
        total=Count("attendance_records"),
        present=Count("attendance_records", filter=Q(attendance_records__status=True)),
    ).order_by("roll_number")
    student_list = list(qs)
    for s in student_list:
        s.percent = pct(s.present, s.total) if s.total else None
        s.low = s.percent is not None and s.percent < LOW_ATTENDANCE_LIMIT
    return render(request, "attendance/students.html", {"students": student_list, "q": q})


def _read_student_form(post):
    return {
        "name": post.get("name", "").strip(),
        "roll_number": post.get("roll_number", "").strip(),
        "department": post.get("department", "").strip(),
        "semester": parse_int(post.get("semester"), minimum=1, maximum=12),
        "section": post.get("section", "A").strip().upper()[:20] or "A",
        "email": post.get("email", "").strip(),
    }


@teacher_required
def add_student(request):
    if request.method == "POST":
        data = _read_student_form(request.POST)
        password = request.POST.get("password", "").strip()
        form = request.POST
        if not (data["name"] and data["roll_number"] and data["department"] and data["email"]):
            messages.error(request, "Name, roll number, department and email are required.")
        elif data["semester"] is None:
            messages.error(request, "Semester must be a number between 1 and 12.")
        elif Student.objects.filter(roll_number__iexact=data["roll_number"]).exists():
            messages.error(request, f"Roll number {data['roll_number']} already exists.")
        elif User.objects.filter(username=data["roll_number"].lower()).exists():
            messages.error(request, f"A login named {data['roll_number'].lower()} already exists.")
        elif password and not check_password_or_message(request, password):
            pass
        else:
            username = data["roll_number"].lower()
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username, email=data["email"],
                    password=password or f"{data['roll_number']}@2026",
                    first_name=data["name"],
                )
                Profile.objects.create(user=user, role="student", department=data["department"])
                Student.objects.create(user=user, teacher=request.user, **data)
            messages.success(request, f"Student added. Username: {username}. Share the password securely.")
            return redirect("students")
        return render(request, "attendance/add_student.html", {"form": form})
    return render(request, "attendance/add_student.html", {"form": {}})


@teacher_required
def edit_student(request, student_id):
    student = get_object_or_404(scoped_students(request.user), id=student_id)
    if request.method == "POST":
        data = _read_student_form(request.POST)
        if not (data["name"] and data["roll_number"] and data["department"] and data["email"]):
            messages.error(request, "Name, roll number, department and email are required.")
        elif data["semester"] is None:
            messages.error(request, "Semester must be a number between 1 and 12.")
        elif Student.objects.filter(roll_number__iexact=data["roll_number"]).exclude(id=student.id).exists():
            messages.error(request, f"Roll number {data['roll_number']} already belongs to another student.")
        else:
            for field, value in data.items():
                setattr(student, field, value)
            student.save()
            if student.user_id:
                student.user.first_name = student.name
                student.user.email = student.email
                student.user.save(update_fields=["first_name", "email"])
            messages.success(request, f"{student.name} updated successfully.")
            return redirect("students")
        return render(request, "attendance/edit_student.html", {"student": student, "form": request.POST})
    form = {
        "name": student.name, "roll_number": student.roll_number, "department": student.department,
        "semester": student.semester, "section": student.section, "email": student.email,
    }
    return render(request, "attendance/edit_student.html", {"student": student, "form": form})


@teacher_required
@require_POST
def delete_student(request, student_id):
    student = get_object_or_404(scoped_students(request.user), id=student_id)
    name, user = student.name, student.user
    with transaction.atomic():
        for sample in student.face_samples.all():
            sample.image.delete(save=False)
        student.delete()
        if user:
            user.delete()
    messages.success(request, f"{name} and their attendance records were deleted.")
    return redirect("students")


@teacher_required
def manage_users(request):
    students_list = scoped_students(request.user).filter(user__isnull=False).select_related("user").order_by("roll_number")
    if request.method == "POST":
        student = get_object_or_404(students_list, id=parse_int(request.POST.get("student_id"), default=0))
        name = request.POST.get("name", "").strip()
        new_username = request.POST.get("username", "").strip().lower()
        password = request.POST.get("password", "").strip()
        if new_username and new_username != student.user.username:
            if User.objects.filter(username=new_username).exclude(id=student.user_id).exists():
                messages.error(request, "That username is already in use.")
                return redirect("manage_users")
            student.user.username = new_username
        if password and not check_password_or_message(request, password, student.user):
            return redirect("manage_users")
        if name:
            student.name = name
            student.user.first_name = name
            student.save(update_fields=["name"])
        if password:
            student.user.set_password(password)
        student.user.save()
        messages.success(request, f"Login profile for {student.name} updated.")
        return redirect("manage_users")
    return render(request, "attendance/manage_users.html", {"students": students_list})


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@teacher_required
def attendance(request):
    assignments = scoped_assignments(request.user)
    assignment_id = parse_int(request.POST.get("assignment") or request.GET.get("assignment"))
    assignment = assignments.filter(id=assignment_id).first() if assignment_id else None

    students_list = scoped_students(request.user)
    if assignment:
        students_list = students_list.filter(section=assignment.section)
    students_list = students_list.order_by("roll_number")

    if request.method == "POST":
        attendance_date = parse_date(request.POST.get("date"))
        if not attendance_date:
            messages.error(request, "Please select a valid attendance date.")
            return redirect("attendance")
        if not assignment:
            messages.error(request, "Please choose a subject and section first.")
            return redirect("attendance")
        subject = assignment.subject
        # Re-saving the same subject/date edits the existing records instead of duplicating them.
        previous = (
            Attendance.objects.filter(session__teacher=request.user, session__subject=subject, date=attendance_date)
            .select_related("session").order_by("-created_at").first()
        )
        with transaction.atomic():
            session = previous.session if previous else CourseSession.objects.create(
                teacher=request.user, subject=subject, assignment=assignment,
                course_code=subject.code, course_name=subject.name,
                room=assignment.room or "—", status="ended",
            )
            for student in students_list:
                is_present = request.POST.get(f"status_{student.id}") == "present"
                Attendance.objects.update_or_create(
                    student=student, date=attendance_date, session=session,
                    defaults={"status": is_present, "source": "manual"},
                )
        messages.success(request, f"Attendance saved for {attendance_date:%d %b %Y}.")
        return redirect(f"{reverse('attendance')}?date={attendance_date.isoformat()}&assignment={assignment.id}")

    selected_date = parse_date(request.GET.get("date"), timezone.localdate())
    existing_qs = Attendance.objects.filter(student__in=students_list, date=selected_date)
    if assignment:
        existing_qs = existing_qs.filter(session__subject=assignment.subject)
    existing = {a.student_id: a.status for a in existing_qs}
    return render(request, "attendance/attendance.html", {
        "rows": [{"student": s, "present": existing.get(s.id, True)} for s in students_list],
        "selected_date": selected_date.isoformat(),
        "already_marked": bool(existing),
        "assignments": assignments,
        "selected_assignment": str(assignment.id) if assignment else "",
    })


@login_required
def attendance_records(request):
    if current_role(request.user) == "student":
        student = current_student(request.user)
        return redirect("student_attendance", student_id=student.id) if student else redirect("dashboard")
    q = request.GET.get("q", "").strip()
    day = parse_date(request.GET.get("date"))
    status = request.GET.get("status", "")
    records = (
        Attendance.objects.filter(student__in=scoped_students(request.user))
        .select_related("student", "session__subject").order_by("-date", "student__roll_number")
    )
    if q:
        records = records.filter(Q(student__name__icontains=q) | Q(student__roll_number__icontains=q))
    if day:
        records = records.filter(date=day)
    if status in ("present", "absent"):
        records = records.filter(status=status == "present")
    page = Paginator(records, 20).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "attendance/attendance_records.html", {
        "records": page,
        "query": params.urlencode(),
        "f": {"q": q, "date": day.isoformat() if day else "", "status": status},
    })


@login_required
def student_attendance(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    role = current_role(request.user)
    owner = current_student(request.user)
    if role == "student" and (not owner or owner.id != student.id):
        messages.error(request, "You can only view your own attendance.")
        return redirect("dashboard")
    if role == "teacher" and not scoped_students(request.user).filter(id=student.id).exists():
        messages.error(request, "This student is not assigned to your class.")
        return redirect("dashboard")

    all_records = Attendance.objects.filter(student=student)
    selected_day = parse_date(request.GET.get("date"))
    selected_subject = parse_int(request.GET.get("subject"))
    records = all_records.select_related("session__subject").order_by("-date")
    if selected_day:
        records = records.filter(date=selected_day)
    if selected_subject:
        records = records.filter(session__subject_id=selected_subject)
    total = records.count()
    present = records.filter(status=True).count()
    percentage = pct(present, total)
    return render(request, "attendance/students_attendance.html", {
        "student": student,
        "records": records,
        "total_days": total,
        "present_days": present,
        "absent_days": total - present,
        "percentage": percentage,
        "low": total > 0 and percentage < LOW_ATTENDANCE_LIMIT,
        "limit": LOW_ATTENDANCE_LIMIT,
        "subject_stats": subject_breakdown(all_records),
        "subjects": Subject.objects.filter(sessions__attendance_records__student=student).distinct(),
        "selected_date": selected_day.isoformat() if selected_day else "",
        "selected_subject": str(selected_subject) if selected_subject else "",
    })


@teacher_required
def attendance_report(request):
    department = request.GET.get("department", "")
    semester = parse_int(request.GET.get("semester"))
    start = parse_date(request.GET.get("start"))
    end = parse_date(request.GET.get("end"))
    only_low = request.GET.get("low") == "1"

    date_q = Q()
    if start:
        date_q &= Q(attendance_records__date__gte=start)
    if end:
        date_q &= Q(attendance_records__date__lte=end)

    base_students = scoped_students(request.user)
    if department:
        base_students = base_students.filter(department=department)
    if semester:
        base_students = base_students.filter(semester=semester)
    student_qs = base_students.annotate(
        total=Count("attendance_records", filter=date_q or None),
        present=Count("attendance_records", filter=(date_q & Q(attendance_records__status=True))),
    ).order_by("roll_number")

    data = []
    for s in student_qs:
        percent = pct(s.present, s.total)
        low = s.total > 0 and percent < LOW_ATTENDANCE_LIMIT
        if not only_low or low:
            data.append({"student": s, "total": s.total, "present": s.present,
                         "absent": s.total - s.present, "percent": percent, "low": low})
    data.sort(key=lambda d: (d["total"] == 0, d["percent"]))

    att_qs = Attendance.objects.filter(student__in=base_students)
    if start:
        att_qs = att_qs.filter(date__gte=start)
    if end:
        att_qs = att_qs.filter(date__lte=end)

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="attendance_report.csv"'
        writer = csv.writer(response)
        writer.writerow(["Roll No", "Name", "Department", "Semester", "Total Classes", "Present", "Absent", "Percentage", "Status"])
        for d in sorted(data, key=lambda item: item["student"].roll_number):
            s = d["student"]
            status = "No data" if d["total"] == 0 else ("Low" if d["low"] else "Good")
            writer.writerow([csv_safe(s.roll_number), csv_safe(s.name), csv_safe(s.department),
                             s.semester, d["total"], d["present"], d["absent"], d["percent"], status])
        return response

    labels, values = weekly_trend(att_qs)
    subject_stats = subject_breakdown(att_qs)
    params = request.GET.copy()
    params.pop("export", None)
    present_records = att_qs.filter(status=True).count()
    total_records = att_qs.count()
    scope = scoped_students(request.user)
    return render(request, "attendance/report.html", {
        "data": data,
        "total_students": len(data),
        "low_count": sum(1 for d in data if d["low"]),
        "limit": LOW_ATTENDANCE_LIMIT,
        "overall": pct(present_records, total_records),
        "present_records": present_records,
        "absent_records": total_records - present_records,
        "total_records": total_records,
        "trend_labels": labels,
        "trend_values": values,
        "subject_labels": [s["code"] for s in subject_stats],
        "subject_values": [s["percent"] for s in subject_stats],
        "departments": scope.values_list("department", flat=True).distinct().order_by("department"),
        "semesters": scope.values_list("semester", flat=True).distinct().order_by("semester"),
        "f": {
            "department": department,
            "semester": str(semester) if semester else "",
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "low": only_low,
        },
        "query": params.urlencode(),
    })


@teacher_required
@require_POST
def send_warnings(request):
    sent = 0
    for item in get_low_students(user=request.user):
        student = item["student"]
        if student.email:
            send_mail(
                "Low Attendance Warning",
                f"Dear {student.name},\n\nYour attendance is {item['percent']}%, below the required "
                f"{LOW_ATTENDANCE_LIMIT}%. Please attend upcoming classes regularly.\n\nAttendIQ",
                None, [student.email], fail_silently=True,
            )
            sent += 1
    if sent:
        messages.success(request, f"Warning emails sent to {sent} student(s).")
    else:
        messages.info(request, "No students are below the limit.")
    return redirect("dashboard")


# --------------------------------------------------------------------------- #
# Face enrollment & live scanning
# --------------------------------------------------------------------------- #
@teacher_required
def face_enroll(request, student_id):
    student = get_object_or_404(scoped_students(request.user), id=student_id)
    if request.method == "POST":
        replace = request.POST.get("replace") == "1"
        candidates = [
            f for f in request.FILES.getlist("samples")[:MAX_FACE_SAMPLES]
            if f.content_type in ALLOWED_IMAGE_TYPES and f.size <= MAX_SAMPLE_BYTES
        ]
        valid, rejected = [], 0
        for image in candidates:
            ok = has_face(image.read())
            image.seek(0)
            if ok:
                valid.append(image)
            else:
                rejected += 1
        existing = 0 if replace else student.face_samples.count()
        if len(valid) + existing < MIN_FACE_SAMPLES:
            messages.error(
                request,
                f"Only {len(valid)} sample(s) contained a clearly detectable face "
                f"({rejected} rejected). Capture at least {MIN_FACE_SAMPLES} well-lit, front-facing frames.",
            )
            return redirect("face_enroll", student_id=student.id)
        with transaction.atomic():
            old_samples = list(student.face_samples.all()) if replace else []
            for image in valid:
                FaceSample.objects.create(student=student, image=image, source="webcam")
            for sample in old_samples:
                sample.image.delete(save=False)
                sample.delete()
            student.face_enrolled, student.face_enrolled_at = True, timezone.now()
            student.save(update_fields=["face_enrolled", "face_enrolled_at"])
        note = f" {rejected} frame(s) without a clear face were skipped." if rejected else ""
        messages.success(request, f"{len(valid)} face sample(s) enrolled for {student.name}.{note}")
        return redirect("students")
    return render(request, "attendance/face_enroll.html", {
        "student": student,
        "sample_count": student.face_samples.count(),
        "min_samples": MIN_FACE_SAMPLES,
        "max_samples": MAX_FACE_SAMPLES,
    })


@teacher_required
def face_scan(request):
    subjects_qs = scoped_subjects(request.user)
    subject_id = parse_int(request.GET.get("subject"))
    selected_subject = subjects_qs.filter(id=subject_id).first() if subject_id else None
    if not selected_subject:
        live = CourseSession.objects.filter(teacher=request.user, status="live").select_related("subject").first()
        selected_subject = live.subject if live and live.subject_id and subjects_qs.filter(id=live.subject_id).exists() else subjects_qs.first()
    session = live_session_for(request.user, selected_subject)
    scope = scoped_students(request.user)
    return render(request, "attendance/face_scan.html", {
        "active_session": session,
        "subjects": subjects_qs,
        "selected_subject": str(session.subject_id or ""),
        "events": ScanEvent.objects.filter(session=session).select_related("student")[:12],
        "total_students": scope.count(),
        "enrolled_count": scope.filter(face_enrolled=True).count(),
        "present_now": Attendance.objects.filter(session=session, status=True).count(),
    })


@teacher_required
@require_http_methods(["POST"])
def face_scan_api(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
        frame = base64.b64decode(str(payload.get("image", "")).split(",", 1)[-1], validate=False)
        subject_id = parse_int(payload.get("subject"))
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return JsonResponse({"ok": False, "message": "Invalid image payload."}, status=400)
    if not frame or len(frame) > MAX_FRAME_BYTES:
        return JsonResponse({"ok": False, "message": "Frame is empty or too large."}, status=400)

    subject = scoped_subjects(request.user).filter(id=subject_id).first() if subject_id else None
    session = live_session_for(request.user, subject or scoped_subjects(request.user).first())
    samples = FaceSample.objects.select_related("student").filter(
        student__in=scoped_students(request.user), student__face_enrolled=True,
    )
    try:
        result, student, confidence, message = match_frame(frame, samples)
    except Exception as exc:  # OpenCV missing / model failure - degrade gracefully
        result, student, confidence, message = "review", None, 0.0, f"OpenCV is not ready: {exc}"

    ScanEvent.objects.create(session=session, student=student, result=result, confidence=confidence)
    already_marked = False
    if result == "matched" and student:
        record, created = Attendance.objects.update_or_create(
            student=student, date=timezone.localdate(), session=session,
            defaults={"status": True, "source": "opencv", "confidence": confidence},
        )
        already_marked = not created
        if already_marked:
            message = "Already marked present for this session."
    return JsonResponse({
        "ok": True,
        "result": result,
        "message": message,
        "student": student.name if student else None,
        "roll_number": student.roll_number if student else None,
        "confidence": round(confidence, 1),
        "already_marked": already_marked,
        "present_now": Attendance.objects.filter(session=session, status=True).count(),
    })


@teacher_required
@require_POST
def end_session(request, session_id):
    session = get_object_or_404(CourseSession, id=session_id, teacher=request.user)
    session.status = "ended"
    session.save(update_fields=["status"])
    if request.POST.get("mark_absent") == "1":
        pool = scoped_students(request.user)
        if session.assignment_id:
            pool = pool.filter(section=session.assignment.section)
        today = timezone.localdate()
        marked = set(Attendance.objects.filter(session=session).values_list("student_id", flat=True))
        absentees = [
            Attendance(student=s, date=today, status=False, session=session, source="auto_absent")
            for s in pool if s.id not in marked
        ]
        Attendance.objects.bulk_create(absentees, ignore_conflicts=True)
        messages.success(request, f"Session ended. {len(absentees)} student(s) not scanned were marked absent.")
    else:
        messages.success(request, "Session ended and audit trail saved.")
    return redirect("dashboard")


# --------------------------------------------------------------------------- #
# Subjects & schedule
# --------------------------------------------------------------------------- #
@teacher_required
def subjects(request):
    subject_list = scoped_subjects(request.user).order_by("teacher__username", "code")
    return render(request, "attendance/subjects.html", {"subjects": subject_list})


@teacher_required
def add_subject(request):
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()
        name = request.POST.get("name", "").strip()
        if not code or not name:
            messages.error(request, "Subject code and name are required.")
        elif Subject.objects.filter(teacher=request.user, code__iexact=code).exists():
            messages.error(request, "You already have a subject with this code.")
        else:
            subject = Subject.objects.create(code=code, name=name, teacher=request.user)
            subject.faculty.add(request.user)
            messages.success(request, f"{code} · {name} added.")
            return redirect("subjects")
    return render(request, "attendance/subject_form.html", {"form": request.POST or {}, "heading": "Add subject"})


@teacher_required
def edit_subject(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)
    if not request.user.is_staff and not (
        subject.teacher_id == request.user.id or subject.faculty.filter(id=request.user.id).exists()
    ):
        messages.error(request, "You can only edit your own subjects.")
        return redirect("subjects")
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()
        name = request.POST.get("name", "").strip()
        if not code or not name:
            messages.error(request, "Subject code and name are required.")
        elif Subject.objects.filter(teacher=subject.teacher, code__iexact=code).exclude(id=subject.id).exists():
            messages.error(request, "Another subject already uses this code.")
        else:
            subject.code, subject.name = code, name
            subject.is_active = request.POST.get("is_active") == "on"
            subject.save()
            messages.success(request, "Subject updated.")
            return redirect("subjects")
    return render(request, "attendance/subject_form.html", {
        "form": {"code": subject.code, "name": subject.name, "is_active": subject.is_active},
        "heading": "Edit subject", "subject": subject,
    })


@teacher_required
def schedules(request):
    return render(request, "attendance/schedules.html", {"assignments": scoped_assignments(request.user)})


@teacher_required
def add_schedule(request):
    subjects_list = scoped_subjects(request.user)
    context = {"subjects": subjects_list, "weekdays": TeachingAssignment.WEEKDAYS, "heading": "Add class schedule"}
    if request.method == "POST":
        subject = subjects_list.filter(id=parse_int(request.POST.get("subject"), default=0)).first()
        start_time = parse_time(request.POST.get("start_time"))
        end_time = parse_time(request.POST.get("end_time"))
        semester = parse_int(request.POST.get("semester"), default=1, minimum=1, maximum=12)
        weekday = parse_int(request.POST.get("weekday"), default=0, minimum=0, maximum=5)
        if not subject:
            messages.error(request, "Please choose a valid subject.")
        elif not (start_time and end_time) or end_time <= start_time:
            messages.error(request, "Enter valid times; the end time must be after the start time.")
        else:
            assignment = TeachingAssignment.objects.create(
                teacher=request.user, subject=subject,
                section=request.POST.get("section", "A").strip().upper()[:20] or "A",
                department=request.POST.get("department", "").strip(), semester=semester, weekday=weekday,
                start_time=start_time, end_time=end_time, room=request.POST.get("room", "").strip(),
            )
            messages.success(request, f"Schedule added for {subject.name}, section {assignment.section}.")
            return redirect("schedules")
    return render(request, "attendance/schedule_form.html", context)


@teacher_required
@require_POST
def delete_schedule(request, assignment_id):
    assignment = get_object_or_404(scoped_assignments(request.user), id=assignment_id)
    assignment.delete()
    messages.success(request, "Class schedule removed.")
    return redirect("schedules")


# --------------------------------------------------------------------------- #
# Help & profile
# --------------------------------------------------------------------------- #
@login_required
def help_center(request):
    return render(request, "attendance/help.html")


@login_required
def profile(request):
    user = request.user
    profile_obj, _ = Profile.objects.get_or_create(
        user=user, defaults={"role": "teacher" if user.is_staff else "student"},
    )
    if request.method == "POST":
        new_password = request.POST.get("password", "").strip()
        if new_password and not check_password_or_message(request, new_password, user):
            return redirect("profile")
        user.first_name = request.POST.get("first_name", "").strip()
        user.last_name = request.POST.get("last_name", "").strip()
        user.email = request.POST.get("email", "").strip()
        profile_obj.title = request.POST.get("title", "").strip()
        profile_obj.department = request.POST.get("department", "").strip()
        profile_obj.phone = request.POST.get("phone", "").strip()
        profile_obj.bio = request.POST.get("bio", "").strip()
        if new_password:
            user.set_password(new_password)
        user.save()
        profile_obj.save()
        if new_password:
            update_session_auth_hash(request, user)
        messages.success(request, "Your profile has been updated.")
        return redirect("profile")
    return render(request, "attendance/profile.html", {"profile_obj": profile_obj})
