import base64
import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Attendance, CourseSession, Profile, Student, Subject, TeachingAssignment


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
