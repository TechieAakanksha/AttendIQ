"""Create demo accounts (and optionally sample students with attendance history)."""
import random
from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from attendance.models import Attendance, CourseSession, Profile, Student, Subject, TeachingAssignment

SUBJECTS = [
    ("DL-301", "Deep Learning Foundations"),
    ("OS-302", "Operating Systems"),
    ("CN-303", "Computer Networks"),
    ("DB-304", "Database Management Systems"),
    ("SE-305", "Software Engineering"),
]

STUDENTS = [
    "Aarav Sharma", "Diya Patel", "Rohan Verma", "Ananya Singh", "Kabir Mehta", "Ishita Gupta",
    "Vihaan Joshi", "Saanvi Reddy", "Arjun Nair", "Meera Iyer", "Yash Kulkarni", "Priya Chauhan",
]

TEACHER_PASSWORD = "AttendIQ@2026"
STUDENT_PASSWORD = "Student@2026"


class Command(BaseCommand):
    help = "Create the demo teacher and subjects. Use --with-students for sample students + 4 weeks of history."

    def add_arguments(self, parser):
        parser.add_argument("--with-students", action="store_true",
                            help="Also create 12 demo students, a weekly timetable and attendance history.")

    @transaction.atomic
    def handle(self, *args, **options):
        teacher, _ = User.objects.get_or_create(
            username="teacher01",
            defaults={"first_name": "Aakanksha", "last_name": "Teacher", "email": "teacher@example.com"},
        )
        teacher.set_password(TEACHER_PASSWORD)
        teacher.save()
        Profile.objects.update_or_create(
            user=teacher, defaults={"role": "teacher", "department": "Computer Science", "title": "Assistant Professor"},
        )
        subjects = []
        for code, name in SUBJECTS:
            subject, _ = Subject.objects.get_or_create(teacher=teacher, code=code, defaults={"name": name})
            subject.faculty.add(teacher)
            subjects.append(subject)
        self.stdout.write(self.style.SUCCESS("Teacher account and starter subjects are ready."))
        self.stdout.write(f"Teacher login: teacher01 / {TEACHER_PASSWORD}")

        if options["with_students"]:
            self._seed_students(teacher, subjects)
        else:
            self.stdout.write("Tip: run with --with-students to load sample students and attendance history.")

    def _seed_students(self, teacher, subjects):
        rng = random.Random(2026)  # deterministic demo data
        students = []
        for index, name in enumerate(STUDENTS, start=1):
            roll = f"CS2026{index:02d}"
            user, created = User.objects.get_or_create(
                username=roll.lower(),
                defaults={"first_name": name, "email": f"{roll.lower()}@example.com"},
            )
            if created:
                user.set_password(STUDENT_PASSWORD)
                user.save()
            Profile.objects.update_or_create(user=user, defaults={"role": "student", "department": "Computer Science"})
            student, _ = Student.objects.get_or_create(
                roll_number=roll,
                defaults={"user": user, "teacher": teacher, "name": name, "department": "Computer Science",
                          "semester": 5, "section": "A", "email": user.email},
            )
            students.append(student)

        assignments = []
        for offset, subject in enumerate(subjects):
            assignment, _ = TeachingAssignment.objects.get_or_create(
                teacher=teacher, subject=subject, section="A", weekday=offset % 5,
                defaults={"department": "Computer Science", "semester": 5, "start_time": time(9 + offset % 3, 0),
                          "end_time": time(10 + offset % 3, 0), "room": f"A-{301 + offset}"},
            )
            assignments.append(assignment)

        if Attendance.objects.filter(student__in=students).exists():
            self.stdout.write("Demo attendance already exists - skipping history generation.")
        else:
            # Each student has a "reliability" so the analytics show a realistic spread.
            reliability = {s.id: rng.choice([0.97, 0.93, 0.9, 0.86, 0.82, 0.7, 0.62]) for s in students}
            today = timezone.localdate()
            created_rows = 0
            for days_back in range(28, 0, -1):
                day = today - timedelta(days=days_back)
                for assignment in assignments:
                    if assignment.weekday != day.weekday():
                        continue
                    session = CourseSession.objects.create(
                        teacher=teacher, subject=assignment.subject, assignment=assignment,
                        course_code=assignment.subject.code, course_name=assignment.subject.name,
                        room=assignment.room, status="ended",
                    )
                    started = timezone.make_aware(datetime.combine(day, assignment.start_time))
                    CourseSession.objects.filter(pk=session.pk).update(session_date=day, starts_at=started)
                    Attendance.objects.bulk_create([
                        Attendance(student=s, date=day, session=session, source="seed_demo",
                                   status=rng.random() < reliability[s.id])
                        for s in students
                    ])
                    created_rows += len(students)
            self.stdout.write(self.style.SUCCESS(f"Created {created_rows} attendance records for {len(students)} students."))

        self.stdout.write(f"Student login example: cs202601 / {STUDENT_PASSWORD}")
        self.stdout.write("Face samples are NOT generated - enroll real, consenting students via Students -> Enroll face.")
