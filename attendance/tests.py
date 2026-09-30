import base64
import io
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Attendance, CourseSession, Profile, Student, Subject, TeachingAssignment
from .qr_utils import make_qr_png, make_session_token, student_id_from_payload, student_qr_payload


def make_teacher(username="teacher01"):
    user = User.objects.create_user(username=username, password="Pass@12345", first_name="Test")
    Profile.objects.create(user=user, role="teacher")
    return user


def make_student(teacher, roll="CS01", username=None):
    user = User.objects.create_user(username=username or roll.lower(), password="Pass@12345", first_name=roll)
    Profile.objects.create(user=user, role="student")
    return Student.objects.create(
        user=user, teacher=teacher, name=f"Student {roll}", roll_number=roll,
        department="CSE", semester=5, section="A", email=f"{roll.lower()}@example.com",
    )


class AccessControlTests(TestCase):
    def setUp(self):
        self.teacher = make_teacher()
        self.student = make_student(self.teacher, "CS01")
        self.other = make_student(self.teacher, "CS02")

    def test_login_required(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_student_cannot_open_teacher_pages(self):
        self.client.login(username="cs01", password="Pass@12345")
        for name in ("students", "attendance", "attendance_report", "face_scan", "subjects", "schedules"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)

    def test_student_cannot_see_other_students(self):
        self.client.login(username="cs01", password="Pass@12345")
        response = self.client.get(reverse("student_attendance", args=[self.other.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("student_attendance", args=[self.student.id])).status_code, 200)

    def test_student_without_record_does_not_crash(self):
        orphan = User.objects.create_user(username="orphan", password="Pass@12345")
        Profile.objects.create(user=orphan, role="student")
        self.client.login(username="orphan", password="Pass@12345")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class TeacherWorkflowTests(TestCase):
    def setUp(self):
        self.teacher = make_teacher()
        self.client.login(username="teacher01", password="Pass@12345")
        self.subject = Subject.objects.create(code="CN-303", name="Computer Networks", teacher=self.teacher)
        self.assignment = TeachingAssignment.objects.create(
            teacher=self.teacher, subject=self.subject, section="A", weekday=0,
            start_time="09:00", end_time="10:00", room="A-1",
        )

    def test_pages_render(self):
        student = make_student(self.teacher)
        for name in ("dashboard", "students", "add_student", "attendance", "attendance_records",
                     "attendance_report", "subjects", "add_subject", "schedules", "add_schedule",
                     "manage_users", "help_center", "profile", "face_scan"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("face_enroll", args=[student.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("edit_student", args=[student.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("student_attendance", args=[student.id])).status_code, 200)

    def test_add_student_creates_login(self):
        response = self.client.post(reverse("add_student"), {
            "name": "Neha Rao", "roll_number": "CS77", "department": "CSE",
            "semester": "3", "section": "b", "email": "neha@example.com", "password": "",
        })
        self.assertRedirects(response, reverse("students"))
        student = Student.objects.get(roll_number="CS77")
        self.assertEqual(student.section, "B")
        self.assertTrue(student.user.check_password("CS77@2026"))

    def test_add_student_rejects_bad_semester_and_duplicates(self):
        payload = {"name": "X", "roll_number": "CS88", "department": "CSE", "semester": "abc",
                   "section": "A", "email": "x@example.com"}
        self.assertEqual(self.client.post(reverse("add_student"), payload).status_code, 200)
        self.assertFalse(Student.objects.filter(roll_number="CS88").exists())
        payload["semester"] = "2"
        self.client.post(reverse("add_student"), payload)
        self.client.post(reverse("add_student"), payload)
        self.assertEqual(Student.objects.filter(roll_number="CS88").count(), 1)

    def test_manual_attendance_is_idempotent(self):
        s1, s2 = make_student(self.teacher, "CS01"), make_student(self.teacher, "CS02")
        today = timezone.localdate().isoformat()
        payload = {"assignment": self.assignment.id, "date": today,
                   f"status_{s1.id}": "present", f"status_{s2.id}": "absent"}
        self.client.post(reverse("attendance"), payload)
        payload[f"status_{s2.id}"] = "present"      # teacher edits and saves again
        self.client.post(reverse("attendance"), payload)
        self.assertEqual(Attendance.objects.count(), 2)          # no duplicates
        self.assertEqual(CourseSession.objects.count(), 1)
        self.assertTrue(Attendance.objects.get(student=s2).status)

    def test_attendance_requires_assignment_and_valid_date(self):
        make_student(self.teacher, "CS01")
        self.assertEqual(self.client.post(reverse("attendance"), {"date": "2026-01-01"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("attendance"), {"assignment": self.assignment.id, "date": "nope"}).status_code, 302)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_bad_query_params_do_not_crash(self):
        student = make_student(self.teacher)
        self.assertEqual(self.client.get(reverse("attendance_records"), {"date": "garbage"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("attendance_report"), {"start": "x", "end": "y", "semester": "q"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("student_attendance", args=[student.id]), {"date": "x", "subject": "y"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("face_scan"), {"subject": "abc"}).status_code, 200)

    def test_csv_export(self):
        make_student(self.teacher)
        response = self.client.get(reverse("attendance_report"), {"export": "csv"})
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("Roll No", response.content.decode())

    def test_duplicate_subject_code_is_handled(self):
        response = self.client.post(reverse("add_subject"), {"code": "cn-303", "name": "Dup"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Subject.objects.filter(teacher=self.teacher).count(), 1)

    def test_schedule_validation(self):
        bad = {"subject": self.subject.id, "section": "A", "semester": "5", "weekday": "1",
               "start_time": "11:00", "end_time": "10:00"}
        self.assertEqual(self.client.post(reverse("add_schedule"), bad).status_code, 200)
        self.assertEqual(TeachingAssignment.objects.count(), 1)
        bad["end_time"] = "12:00"
        self.assertRedirects(self.client.post(reverse("add_schedule"), bad), reverse("schedules"))
        self.assertEqual(TeachingAssignment.objects.count(), 2)

    def test_end_session_marks_absentees(self):
        s1, s2 = make_student(self.teacher, "CS01"), make_student(self.teacher, "CS02")
        session = CourseSession.objects.create(teacher=self.teacher, subject=self.subject, assignment=self.assignment)
        Attendance.objects.create(student=s1, date=timezone.localdate(), session=session, status=True, source="opencv")
        self.client.post(reverse("end_session", args=[session.id]), {"mark_absent": "1"})
        session.refresh_from_db()
        self.assertEqual(session.status, "ended")
        self.assertFalse(Attendance.objects.get(student=s2, session=session).status)
        self.assertTrue(Attendance.objects.get(student=s1, session=session).status)

    def test_scan_api_rejects_garbage_and_handles_blank_frame(self):
        url = reverse("face_scan_api")
        self.assertEqual(self.client.post(url, "not json", content_type="application/json").status_code, 400)
        try:
            import numpy as np
            from PIL import Image
        except ImportError:
            self.skipTest("numpy/Pillow not installed")
        buffer = io.BytesIO()
        Image.fromarray(np.full((240, 320, 3), 128, dtype=np.uint8)).save(buffer, "JPEG")
        image = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
        response = self.client.post(url, {"image": image, "subject": self.subject.id}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.json()["result"], {"no_face", "review"})

    def test_delete_student_removes_login(self):
        student = make_student(self.teacher, "CS55")
        user_id = student.user_id
        self.client.post(reverse("delete_student", args=[student.id]))
        self.assertFalse(Student.objects.filter(id=student.id).exists())
        self.assertFalse(User.objects.filter(id=user_id).exists())

    def test_profile_password_change_keeps_session(self):
        response = self.client.post(reverse("profile"), {"first_name": "New", "password": "Str0ng!Passw0rd#1"})
        self.assertRedirects(response, reverse("profile"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class SeedCommandTests(TestCase):
    def test_seed_with_students_is_repeatable(self):
        call_command("seed_demo", "--with-students", verbosity=0)
        first = Attendance.objects.count()
        self.assertGreater(first, 0)
        call_command("seed_demo", "--with-students", verbosity=0)
        self.assertEqual(Attendance.objects.count(), first)
        self.assertEqual(Student.objects.count(), 12)
        self.assertTrue(Attendance.objects.filter(date__gte=timezone.localdate() - timedelta(days=28)).exists())


def _b64_jpeg(frame):
    import cv2
    ok, jpg = cv2.imencode(".jpg", frame)
    return "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()


def qr_frame(text):
    """A synthetic 'webcam frame' containing a QR code for `text`."""
    import cv2
    import numpy as np
    qr = cv2.imdecode(np.frombuffer(make_qr_png(text), np.uint8), cv2.IMREAD_COLOR)
    qr = cv2.resize(qr, (200, 200), interpolation=cv2.INTER_AREA)
    frame = np.full((480, 640, 3), 120, np.uint8)
    frame[140:340, 220:420] = qr
    return _b64_jpeg(frame)


def blank_frame():
    import numpy as np
    return _b64_jpeg(np.full((240, 320, 3), 128, np.uint8))


class QRAttendanceTests(TestCase):
    def setUp(self):
        self.teacher = make_teacher()
        self.subject = Subject.objects.create(code="CN-303", name="Computer Networks", teacher=self.teacher)
        self.s1 = make_student(self.teacher, "CS01")
        self.s2 = make_student(self.teacher, "CS02")
        self.other_teacher = make_teacher("teacher02")
        self.outsider = make_student(self.other_teacher, "XX99")

    def login_teacher(self):
        self.client.login(username="teacher01", password="Pass@12345")

    def scan(self, **body):
        body.setdefault("subject", self.subject.id)
        return self.client.post(reverse("qr_scan_api"), json.dumps(body), content_type="application/json")

    # --- signed payloads ---------------------------------------------------- #
    def test_payload_round_trip_and_tamper_detection(self):
        payload = student_qr_payload(self.s1)
        self.assertEqual(student_id_from_payload(payload), self.s1.id)
        self.assertIsNone(student_id_from_payload(payload[:-1] + ("A" if payload[-1] != "A" else "B")))
        self.assertIsNone(student_id_from_payload(f"S{self.s2.id}"))          # unsigned / forged
        self.assertIsNone(student_id_from_payload("hello"))
        self.assertIsNone(student_id_from_payload(""))

    def test_qr_png_is_generated(self):
        self.assertTrue(make_qr_png("hello").startswith(b"\x89PNG"))

    # --- teacher scanner API ------------------------------------------------ #
    def test_scan_marks_present_then_reports_duplicate(self):
        self.login_teacher()
        frame = qr_frame(student_qr_payload(self.s1))
        first = self.scan(image=frame).json()
        self.assertEqual(first["result"], "matched")
        self.assertEqual(first["roll_number"], "CS01")
        self.assertEqual(self.scan(image=frame).json()["result"], "duplicate")
        record = Attendance.objects.get(student=self.s1)
        self.assertTrue(record.status)
        self.assertEqual(record.source, "qr")
        self.assertEqual(Attendance.objects.filter(student=self.s1).count(), 1)

    def test_scan_rejects_forged_and_foreign_codes(self):
        self.login_teacher()
        self.assertEqual(self.scan(image=qr_frame(f"S{self.s2.id}")).json()["result"], "invalid")
        self.assertEqual(self.scan(image=qr_frame("https://example.com")).json()["result"], "invalid")
        self.assertEqual(self.scan(image=qr_frame(student_qr_payload(self.outsider))).json()["result"], "unknown")
        self.assertEqual(Attendance.objects.count(), 0)

    def test_scan_frame_without_qr(self):
        self.login_teacher()
        self.assertEqual(self.scan(image=blank_frame()).json()["result"], "no_code")

    def test_scan_bad_payloads(self):
        self.login_teacher()
        url = reverse("qr_scan_api")
        self.assertEqual(self.client.post(url, "nope", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(url, "[1, 2]", content_type="application/json").status_code, 400)
        self.assertEqual(self.scan(image="").status_code, 400)

    def test_manual_roll_entry(self):
        self.login_teacher()
        data = self.scan(roll="cs02").json()
        self.assertEqual(data["result"], "matched")
        self.assertEqual(Attendance.objects.get(student=self.s2).source, "qr_manual")
        self.assertEqual(self.scan(roll="NOPE").json()["result"], "unknown")
        self.assertEqual(self.scan(roll="XX99").json()["result"], "unknown")      # other teacher's student

    def test_scanner_is_teacher_only(self):
        url = reverse("qr_scan_api")
        self.assertEqual(self.client.post(url, "{}", content_type="application/json").status_code, 302)
        self.client.login(username="cs01", password="Pass@12345")
        self.assertEqual(self.client.post(url, json.dumps({"roll": "CS01"}), content_type="application/json").status_code, 302)
        self.assertEqual(Attendance.objects.count(), 0)

    # --- QR images / pages -------------------------------------------------- #
    def test_student_qr_image_permissions(self):
        url = lambda s: reverse("qr_student_image", args=[s.id])
        self.assertEqual(self.client.get(url(self.s1)).status_code, 302)          # anonymous -> login
        self.client.login(username="cs01", password="Pass@12345")
        own = self.client.get(url(self.s1))
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own["Content-Type"], "image/png")
        self.assertEqual(self.client.get(url(self.s2)).status_code, 403)
        self.client.logout()
        self.login_teacher()
        self.assertEqual(self.client.get(url(self.s1)).status_code, 200)
        self.assertEqual(self.client.get(url(self.outsider)).status_code, 403)

    def test_pages_render(self):
        self.login_teacher()
        for name in ("qr_scan", "qr_session", "qr_cards"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("qr_cards"), {"student": self.s1.id, "section": "A"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("qr_cards"), {"student": "abc"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("qr_session"), {"subject": "zzz"}).status_code, 200)
        self.assertRedirects(self.client.get(reverse("my_qr")), reverse("qr_cards"))
        self.client.logout()
        self.client.login(username="cs01", password="Pass@12345")
        self.assertEqual(self.client.get(reverse("my_qr")).status_code, 200)
        self.assertEqual(self.client.get(reverse("qr_scan")).status_code, 302)

    def test_session_qr_image_and_status(self):
        self.login_teacher()
        image = self.client.get(reverse("qr_session_image"))
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image.content.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get(reverse("qr_session_status")).json()["present_now"], 0)

    # --- student phone check-in --------------------------------------------- #
    def _live_session(self):
        self.login_teacher()
        self.client.get(reverse("qr_session"))
        session = CourseSession.objects.get(teacher=self.teacher, status="live")
        self.client.logout()
        return session

    def test_student_checkin_flow(self):
        session = self._live_session()
        token = make_session_token(session)
        self.client.login(username="cs01", password="Pass@12345")
        first = self.client.get(reverse("qr_checkin"), {"t": token})
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "marked present")
        self.assertEqual(Attendance.objects.get(student=self.s1).source, "qr_self")
        self.assertContains(self.client.get(reverse("qr_checkin"), {"t": token}), "Already checked in")
        self.assertEqual(Attendance.objects.filter(student=self.s1).count(), 1)
        # teacher's live status now lists the student
        self.client.logout()
        self.login_teacher()
        status = self.client.get(reverse("qr_session_status")).json()
        self.assertEqual(status["present_now"], 1)
        self.assertEqual(status["recent"][0]["roll"], "CS01")

    def test_checkin_requires_login_and_valid_token(self):
        session = self._live_session()
        token = make_session_token(session)
        self.assertEqual(self.client.get(reverse("qr_checkin"), {"t": token}).status_code, 302)   # -> login
        self.client.login(username="cs01", password="Pass@12345")
        self.assertContains(self.client.get(reverse("qr_checkin"), {"t": "garbage"}), "Invalid QR code")
        self.assertContains(self.client.get(reverse("qr_checkin")), "Invalid QR code")
        with mock.patch("attendance.qr_utils.SESSION_TOKEN_SECONDS", -1):
            self.assertContains(self.client.get(reverse("qr_checkin"), {"t": token}), "QR code expired")
        self.assertEqual(Attendance.objects.count(), 0)

    def test_checkin_blocked_for_ended_session_outsiders_and_teachers(self):
        session = self._live_session()
        token = make_session_token(session)
        self.client.login(username="xx99", password="Pass@12345")                # student of another teacher
        self.assertContains(self.client.get(reverse("qr_checkin"), {"t": token}), "Not in this class")
        self.client.logout()
        self.login_teacher()
        self.assertContains(self.client.get(reverse("qr_checkin"), {"t": token}), "Students only")
        self.client.logout()
        session.status = "ended"
        session.save(update_fields=["status"])
        self.client.login(username="cs01", password="Pass@12345")
        self.assertContains(self.client.get(reverse("qr_checkin"), {"t": token}), "Session ended")
        self.assertEqual(Attendance.objects.count(), 0)
