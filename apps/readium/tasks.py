# Readium Celery Tasks
#
# Note: Content encryption is now triggered directly from License post_save signal
# in apps/readium/models.py. The lcpencrypt worker is called with the License ID,
# and when encryption completes, the webhook creates the LCP UserAcquisition.
#
# See apps/readium/views/hooks.py for the webhook implementation.
