from datetime import datetime, timedelta
from typing import List, Dict, Optional
from django.db.models import Q, Count
from django.utils import timezone

from apps.core.models import Entry, User
from apps.readium.models import License


class LicenseAvailabilityService:
    """Service for managing license availability and calendar data"""
    
    @staticmethod
    def get_entry_availability(entry: Entry, start_date: datetime = None, end_date: datetime = None) -> Dict:
        """
        Get availability information for an entry over a date range
        Returns calendar data suitable for frontend display
        """
        if not entry.read_config('readium_enabled'):
            return {
                'available': False,
                'reason': 'Entry is not readium-enabled',
                'calendar': []
            }
        
        max_concurrent = entry.read_config('readium_amount')
        
        if start_date is None:
            start_date = timezone.now()
        if end_date is None:
            end_date = start_date + timedelta(days=90)  # Default 3 months
        
        # Get all active licenses for this entry in the date range
        active_licenses = License.objects.filter(
            entry=entry,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            starts_at__lte=end_date,
            expires_at__gte=start_date
        ).order_by('starts_at')
        
        # Generate calendar data
        calendar = []
        current_date = start_date.date()
        end_date_only = end_date.date()
        
        while current_date <= end_date_only:
            # Count active licenses for this day
            day_licenses = active_licenses.filter(
                starts_at__date__lte=current_date,
                expires_at__date__gte=current_date
            ).count()
            
            available_slots = max_concurrent - day_licenses
            
            calendar.append({
                'date': current_date.isoformat(),
                'available_slots': max(0, available_slots),
                'total_slots': max_concurrent,
                'is_available': available_slots > 0
            })
            
            current_date += timedelta(days=1)
        
        return {
            'available': any(day['is_available'] for day in calendar),
            'max_concurrent': max_concurrent,
            'calendar': calendar
        }
    
    @staticmethod
    def can_user_borrow(entry: Entry, user: User, start_date: datetime = None, end_date: datetime = None) -> Dict:
        """
        Check if a user can borrow an entry for a specific period
        """
        if not entry.read_config('readium_enabled'):
            return {
                'can_borrow': False,
                'reason': 'Entry is not readium-enabled'
            }
        
        if start_date is None:
            start_date = timezone.now()
        if end_date is None:
            end_date = start_date + timedelta(days=14)  # Default 2 weeks
        
        # Check if user already has an active license for this entry
        existing_license = License.objects.filter(
            entry=entry,
            user=user,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE]
        ).first()
        
        if existing_license:
            return {
                'can_borrow': False,
                'reason': 'User already has an active license for this entry',
                'existing_license': existing_license.pk
            }
        
        max_concurrent = entry.read_config('readium_amount')
        
        # Check availability for the requested period
        conflicting_licenses = License.objects.filter(
            entry=entry,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            starts_at__lt=end_date,
            expires_at__gt=start_date
        ).count()
        
        if conflicting_licenses >= max_concurrent:
            return {
                'can_borrow': False,
                'reason': 'No available slots for the requested period',
                'available_slots': max_concurrent - conflicting_licenses
            }
        
        return {
            'can_borrow': True,
            'available_slots': max_concurrent - conflicting_licenses
        }
    
    @staticmethod
    def create_license_for_user(entry: Entry, user: User, start_date: datetime = None, 
                               duration_days: int = 14, passphrase_hint: str = None,
                               user_passphrase: str = None) -> License:
        """
        Create a new license for a user
        """
        if start_date is None:
            start_date = timezone.now()
        
        end_date = start_date + timedelta(days=duration_days)
        
        # Validate availability
        availability = LicenseAvailabilityService.can_user_borrow(entry, user, start_date, end_date)
        if not availability['can_borrow']:
            raise ValueError(f"Cannot create license: {availability['reason']}")
        
        # Create the license
        license = License.objects.create(
            entry=entry,
            user=user,
            starts_at=start_date,
            expires_at=end_date,
            passphrase_hint=passphrase_hint,
            state=License.LicenseState.READY
        )
        
        # If user passphrase is provided, generate the LCP license immediately
        if user_passphrase:
            from apps.readium.lcp_client import LCPClient
            lcp_client = LCPClient()
            try:
                lcp_license = lcp_client.generate_license(license, user_passphrase)
                lcp_client.notify_status_server(lcp_license)
            except Exception as e:
                # If LCP generation fails, delete the license and raise error
                license.delete()
                raise ValueError(f"Failed to generate LCP license: {str(e)}")
        
        return license