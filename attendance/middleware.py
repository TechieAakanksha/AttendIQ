from django.shortcuts import redirect


class TeacherAdminOnlyMiddleware:
    """Keep the Django admin center out of student accounts even by direct URL."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/admin/") and request.user.is_authenticated and not request.user.is_staff:
            try:
                role = request.user.profile.role
            except Exception:
                role = "student"
            if role != "teacher":
                return redirect("dashboard")
        return self.get_response(request)
