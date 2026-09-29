from django.contrib import admin
from .models import Attendance, CourseSession, FaceSample, Profile, ScanEvent, Student, Subject, TeachingAssignment


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "title", "department", "phone")
    list_filter = ("role", "department")
    search_fields = ("user__username", "user__email")


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ("roll_number", "name", "teacher", "department", "semester", "face_enrolled", "email")
    search_fields = ("name", "roll_number", "email", "department")
    list_filter = ("department", "semester", "face_enrolled")


@admin.register(CourseSession)
class CourseSessionAdmin(admin.ModelAdmin):
    list_display = ("course_code", "course_name", "subject", "room", "teacher", "session_date", "status")
    list_filter = ("status", "session_date", "course_code")
    search_fields = ("course_code", "course_name", "room", "teacher__username")


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ("student", "date", "status", "source", "confidence", "session")
    search_fields = ("student__name", "student__roll_number")
    list_filter = ("date", "status", "source")
    ordering = ("-date", "student__roll_number")


@admin.register(FaceSample)
class FaceSampleAdmin(admin.ModelAdmin):
    list_display = ("student", "source", "created_at")
    list_filter = ("source", "created_at")
    search_fields = ("student__name", "student__roll_number")


@admin.register(ScanEvent)
class ScanEventAdmin(admin.ModelAdmin):
    list_display = ("captured_at", "result", "student", "confidence", "session")
    list_filter = ("result", "captured_at")
    search_fields = ("student__name", "student__roll_number")
    readonly_fields = ("captured_at",)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "teacher", "is_active")
    list_filter = ("is_active", "teacher")
    search_fields = ("code", "name", "teacher__username")


@admin.register(TeachingAssignment)
class TeachingAssignmentAdmin(admin.ModelAdmin):
    list_display = ("subject", "teacher", "section", "weekday", "start_time", "end_time", "room", "is_active")
    list_filter = ("weekday", "is_active", "teacher", "subject")
    search_fields = ("subject__code", "subject__name", "teacher__username", "section")
