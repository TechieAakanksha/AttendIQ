"""Template context available on every page (navigation role flags)."""


def role(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from .views import current_role, current_student

    app_role = current_role(user)
    return {
        "app_role": app_role,
        "is_teacher": app_role == "teacher",
        "nav_student": current_student(user) if app_role == "student" else None,
    }
