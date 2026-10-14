from django.urls import path

from . import views

urlpatterns = [
    path("", views.today, name="today"),
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/<int:pk>/", views.message, name="message"),
    path("calendar/", views.calendar, name="calendar"),
    path("courses/", views.courses, name="courses"),
    path("courses/<str:code>/", views.course, name="course"),
    path("courses/<str:code>/pack/", views.build_pack, name="build_pack"),
    path("capture/", views.capture, name="capture"),
    path("capture/<int:pk>/delete/", views.capture_delete, name="capture_delete"),
    path("capture/check/<int:pk>/", views.chat_check, name="chat_check"),
    path("proposal/<int:pk>/<str:verdict>/", views.proposal_decide, name="proposal_decide"),
    path("question/<int:pk>/", views.answer, name="answer"),
    path("sweep/", views.sweep_now, name="sweep_now"),
    path("done/", views.mark_done, name="mark_done"),
    path("done/<int:pk>/undo/", views.undo_done, name="undo_done"),
    path("status.json", views.status, name="status"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="sw"),
    path("favicon.ico", views.touch_icon, {"size": "192"}),
    path("apple-touch-icon.png", views.touch_icon),
    path("apple-touch-icon-precomposed.png", views.touch_icon),
]
