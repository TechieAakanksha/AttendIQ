from django.contrib.auth.models import User
from django.db import models


class Profile(models.Model):
    ROLE_CHOICES = (("teacher", "Teacher"), ("student", "Student"))
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="student")
    department = models.CharField(max_length=100, blank=True)
    avatar_color = models.CharField(max_length=20, default="#89f7c2")
    title = models.CharField(max_length=80, blank=True, default="Faculty")
    phone = models.CharField(max_length=30, blank=True)
    bio = models.TextField(blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class Subject(models.Model):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, related_name="subjects")
    faculty = models.ManyToManyField(User, related_name="teaching_subjects", blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code", "name"]
        constraints = [models.UniqueConstraint(fields=["teacher", "code"], name="unique_teacher_subject_code")]

    def __str__(self):
        return f"{self.code} · {self.name}"


class Student(models.Model):
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="student_record")
    teacher = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="class_students")
    name = models.CharField(max_length=100)
    roll_number = models.CharField(max_length=20, unique=True)
    department = models.CharField(max_length=100)
    semester = models.IntegerField()
    section = models.CharField(max_length=20, default="A")
    email = models.EmailField()
    face_enrolled = models.BooleanField(default=False)
    face_enrolled_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.roll_number} - {self.name}"


class CourseSession(models.Model):
    STATUS_CHOICES = (("live", "Live"), ("ended", "Ended"), ("scheduled", "Scheduled"))
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, related_name="course_sessions")
    subject = models.ForeignKey(Subject, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions")
    assignment = models.ForeignKey("TeachingAssignment", null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions")
    course_code = models.CharField(max_length=20, default="CS-204")
    course_name = models.CharField(max_length=120, default="Algorithms")
    room = models.CharField(max_length=80, default="A-307")
    session_date = models.DateField(auto_now_add=True)
    starts_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="live")

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return f"{self.course_code} · {self.room} · {self.session_date}"


class TeachingAssignment(models.Model):
    WEEKDAYS = ((0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"), (5, "Saturday"))
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, related_name="teaching_assignments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="teaching_assignments")
    section = models.CharField(max_length=20, default="A")
    department = models.CharField(max_length=100, blank=True)
    semester = models.PositiveSmallIntegerField(default=1)
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS, default=0)
    start_time = models.TimeField()
    end_time = models.TimeField()
    room = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday", "start_time", "section"]

    def __str__(self):
        return f"{self.subject.code} · Section {self.section} · {self.get_weekday_display()} {self.start_time:%H:%M}"


class Attendance(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="attendance_records")
    date = models.DateField()
    status = models.BooleanField(default=True)
    session = models.ForeignKey(CourseSession, null=True, blank=True, on_delete=models.SET_NULL, related_name="attendance_records")
    source = models.CharField(max_length=30, default="manual")
    confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "student__roll_number"]
        constraints = [models.UniqueConstraint(fields=["student", "date", "session"], name="unique_student_date_session")]

    def __str__(self):
        status = "Present" if self.status else "Absent"
        return f"{self.student.roll_number} - {self.student.name} - {self.date} - {status}"


class FaceSample(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="face_samples")
    image = models.ImageField(upload_to="face_samples/%Y/%m/%d/")
    created_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=30, default="webcam")

    def __str__(self):
        return f"{self.student.roll_number} sample {self.pk}"


class ScanEvent(models.Model):
    RESULT_CHOICES = (("matched", "Matched"), ("review", "Needs review"), ("unknown", "Unknown"), ("no_face", "No face"))
    session = models.ForeignKey(CourseSession, null=True, blank=True, on_delete=models.SET_NULL, related_name="scan_events")
    student = models.ForeignKey(Student, null=True, blank=True, on_delete=models.SET_NULL, related_name="scan_events")
    result = models.CharField(max_length=20, choices=RESULT_CHOICES)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at"]

    def __str__(self):
        return f"{self.result} · {self.student or 'Unknown'} · {self.captured_at:%H:%M:%S}"
