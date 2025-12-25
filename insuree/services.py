import base64
import logging
import pathlib
import shutil
import uuid
from importlib import import_module
from os import path

from core.apps import CoreConfig
from django.db.models import Q
from django.utils.translation import gettext as _

from core.signals import register_service_signal
from insuree.apps import InsureeConfig
from insuree.models import (InsureePhoto, PolicyRenewalDetail, Insuree, Family, InsureePolicy, InsureeStatus,
                            InsureeStatusReason, InsureeIdReservation, ReservationStatus, InsureeCheckIn)
from django.core.exceptions import ValidationError
from core.models import filter_validity, resolved_id_reference
from django.db import transaction

logger = logging.getLogger(__name__)
from django.conf import settings
COLLISION_RETRY_ATTEMPTS = getattr(settings, 'INSUREE_COLLISION_RETRY_ATTEMPTS', 20)

def create_insuree_renewal_detail(policy_renewal):
    from core import datetime, datetimedelta
    now = datetime.datetime.now()
    adult_birth_date = now - datetimedelta(years=CoreConfig.age_of_majority)
    photo_renewal_date_adult = now - \
        datetimedelta(months=InsureeConfig.renewal_photo_age_adult)  # 60
    photo_renewal_date_child = now - \
        datetimedelta(months=InsureeConfig.renewal_photo_age_child)  # 12
    photos_to_renew = InsureePhoto.objects.filter(insuree__family=policy_renewal.insuree.family) \
        .filter(insuree__validity_to__isnull=True) \
        .filter(Q(insuree__photo_date__isnull=True)
                | Q(insuree__photo_date__lte=photo_renewal_date_adult)
                | (Q(insuree__photo_date__lte=photo_renewal_date_child)
                   & Q(insuree__dob__gt=adult_birth_date)
                   )
                )
    for photo in photos_to_renew:
        detail, detail_created = PolicyRenewalDetail.objects.get_or_create(
            policy_renewal=policy_renewal,
            insuree_id=photo.insuree_id,
            validity_from=now,
            audit_user_id=0,
        )
        logger.debug("Photo due for renewal for insuree %s, renewal detail %s, created an entry ? %s",
                     photo.insuree_id, detail.id, detail_created)


def custom_insuree_number_validation(insuree_number):
    function_string = InsureeConfig.insuree_number_validator
    try:
        mod, name = function_string.rsplit('.', 1)
        module = import_module(mod)
        function = getattr(module, name)
        return function(insuree_number)
    except ImportError:
        return [{"errorCode": InsureeConfig.validation_code_validator_import_error,
                 "message": _("validator_module_import_error")}]

    except AttributeError:
        return [{"errorCode": InsureeConfig.validation_code_validator_function_error,
                 "message": _("validator_function_not_found")}]


def validate_insuree_number(insuree_number, insuree_uuid=None):
    insuree_number = str(insuree_number)

    if InsureeConfig.insuree_number_validator:
        return custom_insuree_number_validation(insuree_number)
    if InsureeConfig.insuree_number_max_length:
        if not insuree_number:
            return [
                {
                    "errorCode": InsureeConfig.validation_code_no_insuree_number,
                    "message": _("Invalid insuree number (empty), should be %s") %
                    (InsureeConfig.insuree_number_max_length,)
                }
            ]
        if len(insuree_number) > InsureeConfig.insuree_number_max_length:
            return [
                {
                    "errorCode": InsureeConfig.validation_code_invalid_insuree_number_len,
                    "message": _("Invalid insuree number length %s, should be maximun %s") %
                    (
                        len(insuree_number),
                        InsureeConfig.insuree_number_max_length
                    )
                }
            ]
    if InsureeConfig.insuree_number_min_length and len(insuree_number) < InsureeConfig.insuree_number_min_length:
            return [
                {
                    "errorCode": InsureeConfig.validation_code_invalid_insuree_number_len,
                    "message": _("Invalid insuree number length %s, should be minimum %s") %
                    (
                        len(insuree_number),
                        InsureeConfig.insuree_number_min_length
                    )
                }
            ]
        
    config_modulo = InsureeConfig.insuree_number_modulo_root
    if config_modulo:
        try:
            if config_modulo == 10:
                if not is_modulo_10_number_valid(insuree_number):
                    return invalid_checksum()
            else:
                base = int(insuree_number[:-1])
                mod = int(insuree_number[-1])
                if base % config_modulo != mod:
                    return invalid_checksum()
        except Exception as exc:
            logger.exception("Failed insuree number validation", exc)
            return [{"errorCode": InsureeConfig.validation_code_invalid_insuree_number_exception,
                     "message": "Insuree number validation failed"}]
    query = Insuree.objects.filter(
        chf_id=insuree_number, validity_to__isnull=True)
    insuree = query.first()
    if insuree_uuid and insuree and uuid.UUID(insuree.uuid) != uuid.UUID(insuree_uuid):
        return [{
            "errorCode": InsureeConfig.validation_code_taken_insuree_number,
            "message": "Insuree number has to be unique, %s exists in system" % insuree_number
        }]

    
    return []


def is_modulo_10_number_valid(insuree_number: str) -> bool:
    """
    This function checks whether an insuree number is valid, according to the modulo 10 technique.
    Contrarily to its name, this technique does not simply check if number % 10 == 0.
    This function uses Luhn's algorithm (https://en.wikipedia.org/wiki/Luhn_algorithm).
    """
    return (sum(
        (element + (index % 2 == 0) * (element - 9 * (element > 4))
         for index, element in enumerate(map(int, insuree_number[:-1])))
    ) + int(insuree_number[-1])) % 10 == 0


def invalid_checksum():
    return [{"errorCode": InsureeConfig.validation_code_invalid_insuree_number_checksum,
             "message": "Invalid checksum"}]


def reset_insuree_before_update(insuree):
    insuree.family = None
    insuree.last_name = None
    insuree.other_names = None
    insuree.gender = None
    insuree.dob = None
    insuree.head = None
    insuree.marital = None
    insuree.passport = None
    insuree.phone = None
    insuree.email = None
    insuree.current_address = None
    insuree.geolocation = None
    insuree.current_village = None
    insuree.card_issued = None
    insuree.relationship = None
    insuree.profession = None
    insuree.education = None
    insuree.type_of_id = None
    insuree.health_facility = None
    insuree.offline = None
    insuree.json_ext = None


def reset_family_before_update(family):
    family.location = None
    family.poverty = None
    family.family_type = None
    family.address = None
    family.is_offline = None
    family.ethnicity = None
    family.confirmation_no = None
    family.confirmation_type = None
    family.json_ext = None


def handle_insuree_photo(user, now, insuree, data):
    existing_insuree_photo = insuree.photo
    insuree_photo = None
    if not photo_changed(existing_insuree_photo, data):
        return None
    data['audit_user_id'] = user.id_for_audit
    data['validity_from'] = now
    data['insuree_id'] = insuree.id
    photo_bin = data.get('photo', None)
    # no photo changes
    if (
        'uuid' in data and existing_insuree_photo and
        uuid.UUID(data['uuid']) == uuid.UUID(existing_insuree_photo.uuid)
    ):
        existing_insuree_photo_bin = load_photo_file(
            existing_insuree_photo.folder,
            existing_insuree_photo.filename
        )
        if photo_bin == existing_insuree_photo_bin: 
            return existing_insuree_photo
        else:
            # we ignore the uuid, FE must have messup
            data['uuid'] = str(uuid.uuid4())
    if 'uuid' not in data:
        data['uuid'] = str(uuid.uuid4())
    
    
    if photo_bin and InsureeConfig.insuree_photos_root_path \
            and (existing_insuree_photo is None or existing_insuree_photo.photo != photo_bin):
        (file_dir, file_name) = create_file(now, insuree.id, photo_bin, data['uuid'])
        data['folder'] = file_dir
        data['filename'] = file_name
        insuree_photo = InsureePhoto(**data)

    if existing_insuree_photo and insuree_photo:
        existing_insuree_photo.save_history()
        insuree_photo.id = existing_insuree_photo.id
        insuree_photo.date = None
        insuree_photo.officer_id = None
        insuree_photo.folder = None
        insuree_photo.filename = None
        insuree_photo.photo = None
        [setattr(insuree_photo, key, data[key]) for key in data if key != 'id']
    if insuree_photo:
        insuree_photo.save()
    return insuree_photo


def photo_changed(insuree_photo, data):
    return (not insuree_photo and data) or \
        (data and insuree_photo and insuree_photo.date != data.get('date', None)) or \
        (data and insuree_photo and insuree_photo.officer_id != data.get('officer_id', None)) or \
        (data and insuree_photo and insuree_photo.folder != data.get('folder', None)) or \
        (data and insuree_photo and insuree_photo.filename != data.get('filename', None)) or \
        (data and insuree_photo and insuree_photo.photo != data.get('photo', None))


def _photo_dir(file_dir, file_name):
    root = InsureeConfig.insuree_photos_root_path
    return path.join(root, file_dir, file_name)


def _create_dir(file_dir):
    root = InsureeConfig.insuree_photos_root_path
    pathlib.Path(path.join(root, file_dir)) \
        .mkdir(parents=True, exist_ok=True)


def create_file(date, insuree_id, photo_bin, file_name):
    file_dir = path.join(str(date.year), str(date.month),
                         str(date.day), str(insuree_id))
    _create_dir(file_dir)
    with open(_photo_dir(file_dir, file_name), "xb") as f:
        f.write(base64.b64decode(photo_bin))
        f.close()
    return file_dir, file_name


def copy_file(date, insuree_id, original_file):
    file_dir = path.join(str(date.year), str(date.month),
                         str(date.day), str(insuree_id))
    file_name = str(uuid.uuid4())

    _create_dir(file_dir)
    shutil.copy2(original_file, _photo_dir(file_dir, file_name))
    return file_dir, file_name


def load_photo_file(file_dir, file_name):
    photo_path = _photo_dir(file_dir, file_name)
    try:
        with open(photo_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except FileNotFoundError:
        logger.error(f"{photo_path} not found")


def validate_insuree_data(insuree):
    if not insuree.dob:
        raise ValidationError(_("insuree.validation.insuree_requires_dob"))
    if not insuree.gender:
        raise ValidationError(_("insuree.validation.insuree_requires_gender"))
    if not insuree.status:
        raise ValidationError(_("insuree.validation.insuree_requires_status"))



def validate_worker_data(insuree):
    if not insuree.other_names:
        raise ValidationError(_("worker_requires_other_names"))
    if not insuree.last_name:
        raise ValidationError(_("worker_requires_last_name"))


def validate_insuree(insuree):
    # Accept only 9-digit numeric CBHI/CHF IDs (no slashes, no letters)
    import re
    chf_id = insuree.chf_id
    pattern = r"^\d{9}$"
    if not chf_id or not re.match(pattern, chf_id):
        raise ValidationError("invalid_insuree_number: chf_id must be a 9-digit number (e.g., 123456789)")

    if InsureeConfig.insuree_as_worker:
        validate_worker_data(insuree)
    else:
        validate_insuree_data(insuree)


class InsureeIdReservationService:
    def __init__(self, user):
        self.user = user

    def _current_hf_id(self):
        try:
            i_user = getattr(self.user, 'i_user', None)
            return getattr(i_user, 'health_facility_id', None) if i_user else None
        except Exception:
            return None

    def _current_officer(self):
        try:
            from core.models import Officer
            # Common mappings: user.i_user may link to Officer via officer.user
            i_user = getattr(self.user, 'i_user', None)
            if i_user:
                officer = getattr(i_user, 'officer', None)
                if officer:
                    return officer
                # fallback by FK
                off = Officer.objects.filter(user=i_user, validity_to__isnull=True).first()
                if off:
                    return off
            # as last resort, try by user id_for_audit
            off = Officer.objects.filter(validity_to__isnull=True).first()
            return off
        except Exception:
            return None

    @transaction.atomic
    def reserve_new(self, amount: int) -> list:
        if amount <= 0:
            return []
        hf_id = self._current_hf_id()
        officer = self._current_officer()
        reserved = []
        for _ in range(amount):
            chf_id = InsureeService(self.user).generate_unique_chf_id()
            obj = InsureeIdReservation(
                chf_id=chf_id,
                reserved_hf_id=hf_id,
                reserved_officer=officer,
                reserved_by_user_id=getattr(self.user, 'id_for_audit', 0),
                status=ReservationStatus.RESERVED,
                audit_user_id=getattr(self.user, 'id_for_audit', 0),
            )
            obj.save()
            reserved.append(chf_id)
        return reserved

    def get_my(self):
        hf_id = self._current_hf_id()
        officer = self._current_officer()
        qs = InsureeIdReservation.objects.filter(validity_to__isnull=True)
        if hf_id:
            qs = qs.filter(reserved_hf_id=hf_id)
        if officer:
            qs = qs.filter(reserved_officer_id=getattr(officer, 'id', None))
        return qs.order_by('id')

    @transaction.atomic
    def delete_reserved(self, chf_ids: list) -> int:
        if not chf_ids:
            return 0
        hf_id = self._current_hf_id()
        officer = self._current_officer()
        qs = InsureeIdReservation.objects.select_for_update().filter(
            chf_id__in=chf_ids,
            status=ReservationStatus.RESERVED,
            validity_to__isnull=True,
        )
        if hf_id:
            qs = qs.filter(reserved_hf_id=hf_id)
        if officer:
            qs = qs.filter(reserved_officer_id=getattr(officer, 'id', None))
        count = 0
        for r in qs:
            r.status = ReservationStatus.CANCELLED
            r.audit_user_id = getattr(self.user, 'id_for_audit', 0)
            r.save()
            count += 1
        return count

    @transaction.atomic
    def assert_available_and_assign_to_payload(self, reserved_chf_id: str, payload: dict):
        # First, check if the ID exists and is already USED
        existing_any = InsureeIdReservation.objects.filter(
            chf_id=str(reserved_chf_id),
            validity_to__isnull=True,
        ).first()
        if existing_any and existing_any.status == ReservationStatus.USED:
            # Explicitly block using a CHFID that's already used
            raise ValidationError(_("reserved_id_already_used"))

        # Then, check if it's RESERVED and lock the row to assign
        r = InsureeIdReservation.objects.select_for_update().filter(
            chf_id=str(reserved_chf_id),
            status=ReservationStatus.RESERVED,
            validity_to__isnull=True,
        ).first()
        if not r:
            raise ValidationError(_("reserved_id_not_available"))
        # Scope enforcement: same HF/officer as current user if present
        hf_id = self._current_hf_id()
        off = self._current_officer()
        if (r.reserved_hf_id and hf_id and r.reserved_hf_id != hf_id) or \
           (r.reserved_officer_id and off and r.reserved_officer_id != getattr(off, 'id', None)):
            raise ValidationError(_("reserved_id_wrong_scope"))
        # Assign into payload
        payload['chf_id'] = r.chf_id

    @transaction.atomic
    def mark_used(self, reserved_chf_id: str, insuree: Insuree):
        r = InsureeIdReservation.objects.select_for_update().filter(
            chf_id=str(reserved_chf_id),
            status=ReservationStatus.RESERVED,
            validity_to__isnull=True,
        ).first()
        if not r:
            # If already used, explicitly block with a distinct error code
            existing = InsureeIdReservation.objects.filter(
                chf_id=str(reserved_chf_id),
                validity_to__isnull=True,
            ).first()
            if existing and existing.status == ReservationStatus.USED:
                raise ValidationError(_("reserved_id_already_used"))
            # Otherwise, it's simply not available for use
            raise ValidationError(_("reserved_id_not_available"))
        r.status = ReservationStatus.USED
        r.used_by_insuree = insuree
        r.audit_user_id = getattr(self.user, 'id_for_audit', 0)
        r.save()


class InsureeService:
    def __init__(self, user):
        self.user = user

    @register_service_signal('insuree_service.create_or_update')
    def create_or_update(self, data, create_only=False):
        # Extract data that's not part of the Insuree model
        photo_data = data.pop('photo', None)
        add_on_existing_policy = data.pop('add_on_existing_policy', False)
        # chf_id_format is deprecated; generation now uses a simple 9-digit random number
        # chf_id_format = int(data.pop('chf_id_format', 1))
        data.pop('chf_id_format', None)
        no_versioning = bool(data.pop('no_versioning', False))
        
        # Observe is_active but don't remove it from data; let update apply it directly
        # Avoid mapping is_active to status to keep them independent for now
        is_active = True
        data['is_active'] = True
        status = data.get('status')
        if status is None:
            data['status'] = InsureeStatus.ACTIVE
        # If caller explicitly sets is_active, align status accordingly before saving

        # Basic validation
        from core import datetime
        # Derive a safe audit user id (supports DEBUG anonymous calls)
        try:
            from django.contrib.auth.models import AnonymousUser
            if isinstance(self.user, AnonymousUser) or not getattr(self.user, 'id', None):
                audit_id = 0
            else:
                audit_id = self.user.id_for_audit
        except Exception:
            audit_id = 0
        now = datetime.datetime.now()
        data['audit_user_id'] = audit_id
        data['validity_from'] = now
        
        # Validate status
        status = data.get('status', InsureeStatus.ACTIVE)

        # Deprecated: CHF ID format validation removed (we always generate 9-digit IDs)
            
        # Find existing insuree if updating (uuid indicates update intent)
        insuree = None
        uuid_in_payload = "uuid" in data and data.get("uuid")
        original_uuid = data.get("uuid") if uuid_in_payload else None
        if uuid_in_payload:
            # When updating by UUID, ensure we only consider currently valid records
            try:
                insuree = Insuree.objects.filter(uuid=data["uuid"], *filter_validity()).first()
                if not insuree:
                    # Fallback 1: try without validity filter (last historical)
                    insuree = Insuree.objects.filter(uuid=data["uuid"]).order_by('-validity_from').first()
            except Exception as e:
                logger.warning(f"UUID lookup failed for {data.get('uuid')}: {e}")
                insuree = None
        
        # If this is meant to be an update (uuid provided) but no insuree was found, error out clearly
        if not create_only and uuid_in_payload and insuree is None:
            logger.error(f"Update requested but insuree not found. uuid={data.get('uuid')}, chf_id={data.get('chf_id')}")
            raise ValidationError(_("insuree.not_found"))
            
        # Validate photo requirement: only force a photo when creating or when insuree has no existing photo
        if InsureeConfig.is_insuree_photo_required:
            creating = insuree is None
            missing_existing_photo = (insuree is not None and getattr(insuree, 'photo', None) is None)
            if (creating or missing_existing_photo) and photo_data is None:
                raise ValidationError(_("mutation.insuree.no_required_photo"))

        
        # Validate health facility requirement
        if InsureeConfig.insuree_fsp_mandatory and 'health_facility_id' not in data:
            raise ValidationError("mutation.insuree.fsp_required")

        # Reserved CHFID support (from offline pre-reservation)
        reserved_chf_id = data.pop('reserved_chf_id', None)

        # Treat any provided chf_id as legacy (from manual system) and always generate a fresh chf_id
        creating = insuree is None
        provided_chf = data.get('chf_id')
        if creating:
            if reserved_chf_id:
                # Enforce that reserved_chf_id belongs to current user context and is still RESERVED
                InsureeIdReservationService(self.user).assert_available_and_assign_to_payload(reserved_chf_id, data)
            else:
                if provided_chf:
                    # Store in dedicated column for reporting/filtering
                    data['legacy_chf_id'] = provided_chf
                try:
                    data['chf_id'] = self.generate_unique_chf_id()
                except Exception as e:
                    logger.error("Failed to generate CHFID on create, leaving provided value as-is. Error: %s", e)
        else:
            # On update, never regenerate or overwrite CHFID; drop any incoming chf_id from payload
            if 'chf_id' in data:
                data.pop('chf_id', None)
                logger.debug("Ignored chf_id in update payload to preserve existing CHFID for insuree uuid=%s", insuree.uuid)

        # Disable policies if needed
        if insuree:
            self.disable_policies_of_insuree(insuree=insuree, status_date=data.get('status_date', now.date()))
            
        # Create new insuree or update existing one
        if not insuree:
            insuree = Insuree(**data)
        else:
            # If caller toggles is_active, force in-place update to avoid history/versioning
            # interfering with persisting IsActive
            force_in_place = bool(is_active is not None) or bool(no_versioning)
            if force_in_place:
                # Update the current row in place, without saving history/new version
                insuree = self._update(insuree, data, in_place=True)
            else:
                insuree = self._update(insuree, data)
        
        insuree.save()
        insuree.refresh_from_db()

        # Handle photo if provided
        if photo_data:
            photo = handle_insuree_photo(self.user, insuree.validity_from, insuree, photo_data)
            if photo:
                insuree.photo = photo
                insuree.photo_date = photo.date
                insuree.save()

        # If using a reserved CHFID, mark it USED now that the insuree exists
        try:
            if creating and reserved_chf_id:
                InsureeIdReservationService(self.user).mark_used(reserved_chf_id, insuree)
        except Exception as e:
            logger.error("Failed to mark reserved CHFID %s as used: %s", reserved_chf_id, e)

        # Apply explicit is_active toggle if it was provided in the payload.
        # Apply it reliably by targeting the most recent row for this UUID, in case versioning changed the PK.
        
        # Activate policies if requested
        if add_on_existing_policy:
            self.activate_policies_of_insuree(insuree=insuree)
            
        return insuree

    def generate_unique_chf_id(self) -> str:
        """Generate a unique 9-digit numeric CHFID (no slashes, no letters)."""
        import random
        rng = random.SystemRandom()
        for _ in range(COLLISION_RETRY_ATTEMPTS):
            candidate = f"{rng.randint(100000000, 999999999)}"  # 9 digits
            # Must not collide with active insurees nor with reserved-but-unused IDs
            exists_in_insuree = Insuree.objects.filter(chf_id=candidate, validity_to__isnull=True).exists()
            exists_in_reserved = InsureeIdReservation.objects.filter(
                chf_id=candidate, status=ReservationStatus.RESERVED, validity_to__isnull=True
            ).exists()
            if not exists_in_insuree and not exists_in_reserved:
                return candidate
        # Fallback: derive 9 digits from UUID hex
        digits = ''.join(ch for ch in uuid.uuid4().hex if ch.isdigit())
        if len(digits) < 9:
            digits = (digits + '0'*9)[:9]
        return digits[:9]

    def disable_policies_of_insuree(self, insuree, status_date):
        """Placeholder: disable policies logic.
        Intentionally minimal to avoid blocking insuree updates. Extend with real policy
        disabling rules if required by business logic.
        """
        try:
            logger.info("disable_policies_of_insuree called for insuree id=%s on %s", insuree.id, status_date)
        except Exception as e:
            logger.error("disable_policies_of_insuree error: %s", e)

    def activate_policies_of_insuree(self, insuree):
        """Activate or add policies on existing insuree if requested.
        Delegates to InsureePolicyService; errors are logged but non-fatal.
        """
        try:
            InsureePolicyService(self.user).add_insuree_policy(insuree)
        except Exception as e:
            logger.error("activate_policies_of_insuree error: %s", e)

    def  _update(self, insuree, data, in_place=True):
        h_id = insuree.save_history()
        if not in_place:
            # reset the non required fields
            # (each update is 'complete', necessary to be able to set 'null')
            reset_insuree_before_update(insuree)
        # Avoid overwriting identifiers inadvertently during update
        # Protect id, uuid, and chf_id so updates cannot change the CHFID
        protected_keys = {"id", "uuid", "chf_id"}
        for key, value in data.items():
            if key in protected_keys:
                continue
            setattr(insuree, key, value)
        return insuree
    
    def remove(self, insuree):
        try:
            insuree.save_history()
            insuree.family = None
            insuree.save()
            return []
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_remove_insuree")
            return {
                'title': insuree.chf_id,
                'list': [{
                    'message': _("insuree.mutation.failed_to_remove_insuree") % {'chfid': insuree.chfid},
                    'detail': insuree.uuid}]
            }
    
     
    @register_service_signal('insuree_service.delete')
    def set_deleted(self, insuree):
        try:
            insuree.delete_history()
            [ip.delete_history()
             for ip in insuree.insuree_policies.filter(validity_to__isnull=True)]
            return []
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_delete_insuree")
            return {
                'title': insuree.chf_id,
                'list': [{
                    'message': _("insuree.mutation.failed_to_delete_insuree") % {'chfid': insuree.chf_id},
                    'detail': insuree.uuid}]
            }
    def checkin_insuree(self, data):
        from core import datetime
        from datetime import timedelta
        from django.utils import timezone
        now = timezone.now()
        try:
            insuree = Insuree.objects.filter(uuid=data["uuid"], *filter_validity()).first()
        except Exception as e:
            logger.warning(f"UUID lookup failed for {data.get('uuid')}: {e}")
            insuree = None

        if not insuree:
            raise ValidationError(_("insuree.validation.id_does_not_exist"))

        health_facility = getattr(self.user, 'health_facility', None)
        if health_facility is None:
            raise ValidationError(
                _("Receptionist accounts must be assigned to a Health Facility before they can perform insuree check-ins.")
            )
        
        last_24_hours = now - timedelta(hours=24)
        already_checked_in = InsureeCheckIn.objects.filter(
            insuree=insuree,
            check_in_date__gte=last_24_hours
        ).exists()
        if already_checked_in:
            raise ValidationError(_("This insuree has already been checked in within the last 24 hours."))
        
        audit_user_id = getattr(self.user, 'id_for_audit', 0)
        InsureeCheckIn.objects.create(
            insuree=insuree,
            health_facility=health_facility,
            check_in_date=now,
            audit_user_id=audit_user_id
        )

        return insuree

class InsureePolicyService:
    def __init__(self, user):
        self.user = user

    def add_insuree_policy(self, insuree):
        from policy.models import Policy
        policies = Policy.objects.filter(family_id=insuree.family_id)
        for policy in policies:
            can_add = getattr(policy, 'can_add_insuree', None)
            if callable(can_add) and policy.can_add_insuree():
                ip = InsureePolicy(
                    insuree=insuree,
                    policy=policy,
                    enrollment_date=policy.enroll_date,
                    start_date=policy.start_date,
                    effective_date=policy.effective_date,
                    expiry_date=policy.expiry_date,
                    offline=False,
                    audit_user_id=getattr(self.user, 'id_for_audit', 0),
                )
                ip.save()


class FamilyService:
    def __init__(self, user):
        self.user = user

    def create_or_update(self, data):
        head_insuree_data = data.pop('head_insuree', None)
        
        if head_insuree_data:
            head_insuree_data.pop('disability_status', None)
            head_insuree_data["head"] = True
            head_insuree = InsureeService(
                self.user).create_or_update(head_insuree_data)
            data["head_insuree_id"] = head_insuree.id
        
        elif 'head_insuree_id' not in data:
            raise Exception(f'no head insuree found')
        from core import datetime

        now = datetime.datetime.now()

        data['audit_user_id'] = self.user.id_for_audit
        data['validity_from'] = now
        family = Family(**data)
        return self._create_or_update(family)

    def _create_or_update(self, family):
        if family.id:
            filters = Q(id=family.id)
            # remove it from now3 to avoid id at creation
            family.id = None
        elif family.uuid:
            filters = Q(uuid=(family.uuid))
        else:
            filters = None
        existing_family = Family.objects.filter(*filter_validity(), filters).first() if filters else None
        if existing_family:
            return self._update(existing_family, family)
        else:
            return self._create(family)

    def _create(self, family):
        family.save()
        family.head_insuree.family = family
        family.head_insuree.save()
        return family

    def _update(self, existing_family, family):
        existing_family.save_history()
        family.id = existing_family.id
        family.save()
        if family.head_insuree.family != family:
            family.head_insuree.family = family
            family.head_insuree.save()
        return family

    def set_deleted(self, family, delete_members):
        try:
            [self.handle_member_on_family_delete(member, delete_members)
             for member in family.members.filter(validity_to__isnull=True).all()]
            family.delete_history()
            return []
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_delete_family")
            return {
                'title': family.uuid,
                'list': [{
                    'message': _("insuree.mutation.failed_to_delete_family") % {'chfid': family.chfid},
                    'detail': family.uuid}]
            }

    def handle_member_on_family_delete(self, member, delete_members):
        insuree_service = InsureeService(self.user)
        if delete_members:
            insuree_service.set_deleted(member)
        else:
            insuree_service.remove(member)
