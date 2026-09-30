from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from attendance import views

admin.site.site_header = "AttendIQ Administration"
admin.site.site_title = "AttendIQ Admin"
admin.site.index_title = "System management"

urlpatterns = [
    path("admin/", admin.site.urls),

    # Authentication
    path("login/", auth_views.LoginView.as_view(template_name="attendance/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    # Workspace
    path("", views.dashboard, name="dashboard"),
    path("help/", views.help_center, name="help_center"),
    path("profile/", views.profile, name="profile"),

    # Students
    path("students/", views.students, name="students"),
    path("students/add/", views.add_student, name="add_student"),
    path("students/<int:student_id>/edit/", views.edit_student, name="edit_student"),
    path("students/<int:student_id>/delete/", views.delete_student, name="delete_student"),
    path("users/manage/", views.manage_users, name="manage_users"),

    # Subjects & timetable
    path("subjects/", views.subjects, name="subjects"),
    path("subjects/add/", views.add_subject, name="add_subject"),
    path("subjects/<int:subject_id>/edit/", views.edit_subject, name="edit_subject"),
    path("schedules/", views.schedules, name="schedules"),
    path("schedules/add/", views.add_schedule, name="add_schedule"),
    path("schedules/<int:assignment_id>/delete/", views.delete_schedule, name="delete_schedule"),

    # Attendance
    path("attendance/", views.attendance, name="attendance"),
    path("attendance/records/", views.attendance_records, name="attendance_records"),
    path("attendance/report/", views.attendance_report, name="attendance_report"),
    path("attendance/warnings/", views.send_warnings, name="send_warnings"),
    path("attendance/student/<int:student_id>/", views.student_attendance, name="student_attendance"),

    # Face recognition
    path("face/enroll/<int:student_id>/", views.face_enroll, name="face_enroll"),
    path("face/scan/", views.face_scan, name="face_scan"),
    path("face/scan/api/", views.face_scan_api, name="face_scan_api"),
    path("sessions/<int:session_id>/end/", views.end_session, name="end_session"),

    # QR-code attendance
    path("qr/scan/", views.qr_scan, name="qr_scan"),
    path("qr/scan/api/", views.qr_scan_api, name="qr_scan_api"),
    path("qr/session/", views.qr_session, name="qr_session"),
    path("qr/session/image/", views.qr_session_image, name="qr_session_image"),
    path("qr/session/status/", views.qr_session_status, name="qr_session_status"),
    path("qr/checkin/", views.qr_checkin, name="qr_checkin"),
    path("qr/me/", views.my_qr, name="my_qr"),
    path("qr/cards/", views.qr_cards, name="qr_cards"),
    path("qr/student/<int:student_id>.png", views.qr_student_image, name="qr_student_image"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
