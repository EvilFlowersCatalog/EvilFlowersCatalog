from django.core.management.base import BaseCommand
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = "Sets an unusable password for the given username"

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username of the user")

    def handle(self, *args, **kwargs):
        username = kwargs["username"]
        try:
            user = User.objects.get(username=username)

            user.set_unusable_password()
            user.save()

            self.stdout.write(self.style.SUCCESS(f"Successfully set an unusable password for user '{username}'"))

        except User.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"User with username '{username}' does not exist."))
