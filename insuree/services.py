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
                            InsureeStatusReason)
from django.core.exceptions import ValidationError
from core.models import filter_validity, resolved_id_reference

logger = logging.getLogger(__name__)


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
    insuree.chf_id = None
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
    # Accept new insurance number format: region_code/auto_id/member_no/year/admin_id
    import re
    chf_id = insuree.chf_id
    # Example: ኮቀ/0001/1/09/125
    # Accept three flexible formats:
    # 1. region/district/auto_id/family_no/admin
    # 2. district/auto_id/family_no/admin
    # 3. auto_id/family_no/admin
    # Accept the three flexible formats ending with 4-digit year
    pattern = r"^((?:[\w\u1200-\u137F]{2,}/){2}\d{4,}/\d+/\d+/\d{4}|\d{4,}/[\w\u1200-\u137F]{2,}/\d+/\d+/\d{4}|\d{4,}/\d+/\d+/\d{4})$"
    if not chf_id or not re.match(pattern, chf_id):
        raise ValidationError("invalid_insuree_number: chf_id must be in the format region_code/auto_id/member_no/year/admin_id, e.g., ኮቀ/0001/1/09/125")

    if InsureeConfig.insuree_as_worker:
        validate_worker_data(insuree)
    else:
        validate_insuree_data(insuree)


class InsureeService:
    def __init__(self, user):
        self.user = user

    @register_service_signal('insuree_service.create_or_update')
    def create_or_update(self, data, create_only=False):
        # Extract data that's not part of the Insuree model
        photo_data = data.pop('photo', None)
        add_on_existing_policy = data.pop('add_on_existing_policy', False)
        chf_id_format = int(data.pop('chf_id_format', 1))  # 1=region+district+auto, 2=district+auto, 3=auto only
        no_versioning = bool(data.pop('no_versioning', False))
        
        # Observe is_active but don't remove it from data; let update apply it directly
        # Avoid mapping is_active to status to keep them independent for now
        is_active = data.get('is_active', None)
        
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
        if status not in [choice[0] for choice in InsureeStatus.choices]:
            raise ValidationError(_("mutation.insuree.wrong_status"))
            
        # Validate CHF ID format
        if chf_id_format not in [1, 2, 3]:
            raise ValidationError(_("invalid_chf_id_format"))
            
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

        # Handle status-specific validation and actions
        if status in [InsureeStatus.INACTIVE, InsureeStatus.DEAD]:
            # For inactive insurees created via isActive=false, we've already set a hardcoded status reason
            # For other cases, validate the status reason
            if is_active is False:
                # Try to get the status reason object for our hardcoded value
                try:
                    status_reason = InsureeStatusReason.objects.filter(
                        validity_to__isnull=True
                    ).first()
                    if status_reason:
                        # Use this status reason regardless of its type
                        data['status_reason'] = status_reason
                    else:
                        # If no status reasons exist at all, create a dummy one in memory
                        # This won't be saved to the database but will allow the code to continue
                        logger.warning("No status reasons found in database, using dummy reason")
                        class DummyReason:
                            pass
                        status_reason = DummyReason()
                        status_reason.code = '1'
                        data['status_reason'] = status_reason
                except Exception as e:
                    logger.error(f"Error finding status reason: {e}")
                    # Continue anyway
            else:
                # For normal status changes (not via isActive), use the standard validation
                try:
                    status_reason = InsureeStatusReason.objects.get(
                        code=data.get('status_reason', None),
                        validity_to__isnull=True
                    )
                    if status_reason is None or status_reason.status_type != status:
                        raise ValidationError(_("mutation.insuree.wrong_status"))
                    data['status_reason'] = status_reason
                except InsureeStatusReason.DoesNotExist:
                    raise ValidationError(_("mutation.insuree.wrong_status"))
            
            # Disable policies if needed
            if insuree:
                self.disable_policies_of_insuree(insuree=insuree, status_date=data.get('status_date', now.date()))
                
        # Validate health facility requirement
        if InsureeConfig.insuree_fsp_mandatory and 'health_facility_id' not in data:
            raise ValidationError("mutation.insuree.fsp_required")

        # Treat any provided chf_id as legacy (from manual system) and always generate a fresh chf_id
        provided_chf = data.get('chf_id')
        if provided_chf:
            # Store in dedicated column for reporting/filtering
            data['legacy_chf_id'] = provided_chf
        try:
            data['chf_id'] = self.generate_unique_chf_id(data, chf_id_format)
        except Exception as e:
            logger.error("Failed to generate CHFID, leaving provided value as-is. Error: %s", e)

        # Disable policies if needed
        if insuree:
            self.disable_policies_of_insuree(insuree=insuree, status_date=data.get('status_date', now.date()))
            
        # Create new insuree or update existing one
        if not insuree:
            insuree = Insuree(**data)
        else:
            if no_versioning:
                # Update the current row in place, without saving history/new version
                self._update(insuree, data, in_place=True)
            else:
                self._update(insuree, data)
        insuree.save()
        insuree.refresh_from_db()

        # Handle photo if provided
        if photo_data:
            photo = handle_insuree_photo(self.user, insuree.validity_from, insuree, photo_data)
            if photo:
                insuree.photo = photo
                insuree.photo_date = photo.date
                insuree.save()

        # Apply explicit is_active toggle if it was provided in the payload.
        # We popped is_active earlier to avoid interfering with status validation.
        # Here we apply it directly to the currently valid row.
        try:
            if is_active is not None:
                # Re-fetch the actual current row, as versioning may have created a new UUID
                current = None
                if getattr(insuree, 'chf_id', None):
                    current = Insuree.objects.filter(chf_id=insuree.chf_id, validity_to__isnull=True).order_by('-validity_from').first()
                if not current:
                    current = Insuree.objects.filter(uuid=insuree.uuid, validity_to__isnull=True).first()
                if current:
                    logger.info("Applying is_active=%s to current insuree row id=%s uuid=%s", is_active, current.id, current.uuid)
                    # Keep status consistent with is_active when explicitly toggled
                    target_status = InsureeStatus.INACTIVE if is_active is False else InsureeStatus.ACTIVE
                    updated = Insuree.objects.filter(id=current.id, validity_to__isnull=True).update(
                        is_active=is_active, status=target_status, audit_user_id=audit_id
                    )
                    logger.info("is_active update result for insuree id=%s uuid=%s: %s row(s) updated", current.id, current.uuid, updated)
                    if updated:
                        insuree = current
                        insuree.refresh_from_db()
                    # Additionally, if the mutation addressed a specific UUID and it differs from the current row,
                    # sync the is_active (and status) there as well so UUID-based queries reflect the toggle.
                    if original_uuid and (not current or str(current.uuid) != str(original_uuid)):
                        try:
                            sync_count = Insuree.objects.filter(uuid=original_uuid).update(
                                is_active=is_active, status=target_status, audit_user_id=audit_id
                            )
                            logger.info("Synchronized is_active to input uuid=%s: %s row(s) updated", original_uuid, sync_count)
                        except Exception as se:
                            logger.warning("Failed to sync is_active for original uuid=%s: %s", original_uuid, se)
                else:
                    logger.warning("Could not locate current insuree row to apply is_active. uuid=%s chf_id=%s", insuree.uuid, getattr(insuree, 'chf_id', None))
        except Exception as e:
            logger.error(f"Failed to apply is_active={is_active} for insuree {getattr(insuree, 'id', None)}: {e}")

        # Activate policies if requested
        if add_on_existing_policy:
            self.activate_policies_of_insuree(insuree=insuree)
            
        return insuree

    def generate_unique_chf_id(self, data: dict, chf_id_format: int = 1) -> str:
        """Generate a CHFID matching accepted patterns and ensure uniqueness.
        chf_id_format:
          1 = region/district/auto/member/admin/year
          2 = district/auto/member/admin/year
          3 = auto/member/admin/year
        Fallbacks (RG/DS) are used if no location context is available.
        """
        from core import datetime
        # Try to infer region/district from provided family/location references
        region = 'RG'
        district = 'DS'
        try:
            # When creating, data may include family or current_village; try family first
            fam = data.get('family') or data.get('family_id')
            village = data.get('current_village') or data.get('current_village_id')
            location_obj = None
            if fam and isinstance(fam, Family):
                location_obj = getattr(fam, 'location', None)
            # Fallback to insuree.current_village's district
            if not location_obj and village and hasattr(village, 'parent'):
                # village.parent -> ward, village.parent.parent -> district
                location_obj = getattr(village, 'parent', None)
                if location_obj:
                    location_obj = getattr(location_obj, 'parent', None)
            # Derive abbreviations
            if location_obj and hasattr(location_obj, 'parent') and getattr(location_obj, 'parent', None):
                # location_obj is district; region is parent
                region = _abbr(getattr(location_obj.parent, 'name', None)) or region
                district = _abbr(getattr(location_obj, 'name', None)) or district
        except Exception as e:
            logger.debug("CHFID location derivation failed: %s", e)

        # Member number: 1 for head if provided in data, else 1
        member_no = 1
        try:
            if 'head' in data and data.get('head') is True:
                member_no = 1
            elif 'relationship' in data and getattr(data.get('relationship'), 'id', None):
                # if relationship is available, assign 2 as a generic non-head index
                member_no = 2
        except Exception:
            pass

        admin_id = 0
        try:
            admin_id = getattr(self.user, 'id_for_audit', 0) or 0
        except Exception:
            admin_id = 0

        year = datetime.datetime.now().year

        # Build candidate according to format
        def build_candidate(seq: str) -> str:
            return seq
        # auto: 6-digit random-like increasing suffix to reduce collision risk
        import random
        for _ in range(20):  # try up to 20 times to avoid rare collisions
            auto_id = f"{random.randint(100000, 999999)}"
            if chf_id_format == 1:
                candidate = f"{region}/{district}/{auto_id}/{member_no}/{admin_id}/{year}"
            elif chf_id_format == 2:
                candidate = f"{district}/{auto_id}/{member_no}/{admin_id}/{year}"
            else:
                candidate = f"{auto_id}/{member_no}/{admin_id}/{year}"

            # Ensure uniqueness against current valid insurees
            if not Insuree.objects.filter(chf_id=candidate, validity_to__isnull=True).exists():
                return candidate

        # As a last resort, append a UUID fragment
        uuid_fragment = uuid.uuid4().hex[:6]
        if chf_id_format == 1:
            fallback = f"{region}/{district}/{uuid_fragment}/{member_no}/{admin_id}/{year}"
        elif chf_id_format == 2:
            fallback = f"{district}/{uuid_fragment}/{member_no}/{admin_id}/{year}"
        else:
            fallback = f"{uuid_fragment}/{member_no}/{admin_id}/{year}"
        return fallback

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

    def _update(self, insuree, data, in_place=False):
        if not in_place:
            insuree.save_history()
            # reset the non required fields
            # (each update is 'complete', necessary to be able to set 'null')
            reset_insuree_before_update(insuree)
        # Avoid overwriting identifiers inadvertently during update
        # Keep id and uuid protected, but allow chf_id to be set from payload to avoid clearing it
        protected_keys = {"id", "uuid"}
        for key, value in data.items():
            if key in protected_keys:
                continue
            setattr(insuree, key, value)
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
