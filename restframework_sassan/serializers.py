import time

from django.db.models import Model
from django.db.models.fields.related import (ManyToManyField, ManyToManyRel,
                                             ManyToOneRel)
from django.db.models.fields.related import ManyToManyField
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.serializers import ListSerializer


class UnixEpochDateTimeField(serializers.DateTimeField):
    def to_representation(self, value):
        try:
            return int(value.timestamp())
        except (AttributeError, TypeError):
            return None

    def to_internal_value(self, value):
        return timezone.datetime.fromtimestamp(int(value), tz=timezone.utc)


class UnixEpochDateField(serializers.DateField):
    def to_representation(self, value):
        try:
            return int(time.mktime(value.timetuple()))
        except (AttributeError, TypeError):
            return None

    def to_internal_value(self, value):
        return timezone.datetime.fromtimestamp(int(value), tz=timezone.utc).\
            date()


class TimeField(serializers.TimeField):
    def to_representation(self, value):
        try:
            return value.hour * 3600 + value.minute * 60 + value.second
        except (AttributeError, TypeError):
            return None

    def to_internal_value(self, value):
        return timezone.datetime.fromtimestamp(int(value), tz=timezone.utc).\
            time()


class DurationField(serializers.DateTimeField):
    def to_representation(self, value):
        try:
            return value.total_seconds()
        except (AttributeError, TypeError):
            return None

    def to_internal_value(self, value):
        return timezone.timedelta(seconds=value)


class BulkListSerializer(ListSerializer):
    update_lookup_field = 'id'

    def update(self, queryset, all_validated_data):
        id_attr = getattr(self.child.Meta, 'update_lookup_field', 'id')

        all_validated_data_by_id = {
            int(i.pop(id_attr)): i
            for i in all_validated_data
        }

        self.objects_to_update = queryset.filter(**{
            '{}__in'.format(id_attr): all_validated_data_by_id.keys(),
        })

        if len(all_validated_data_by_id) != self.objects_to_update.count():
            raise ValidationError(_('Could not find all objects to update.'))

        updated_objects = []

        for obj in self.objects_to_update:
            obj_id = getattr(obj, id_attr)
            obj_validated_data = all_validated_data_by_id.get(obj_id)

            updated_objects.append(self.child.update(obj, obj_validated_data))

        return updated_objects


class BaseModelSerializer(serializers.ModelSerializer):
    class Meta:
        list_serializer_class = BulkListSerializer

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        fields = kwargs.pop('fields', None)
        if fields:
            fields = fields.split(',')
            allowed = set(fields)
            existing = set(self.fields.keys())
            for field_name in existing - allowed:
                self.fields.pop(field_name)

    def run_validators(self, value):
        to_validate = self._read_only_defaults()
        if not isinstance(value, Model):
            to_validate.update(value)
        super().run_validators(to_validate)

    def to_internal_value(self, data):
        id_attr = getattr(self.Meta, 'update_lookup_field', 'id')
        if (
            not self.parent or
            isinstance(self.parent, serializers.ListSerializer) and
            not self.parent.parent
        ):
            ret = super().to_internal_value(data)
        elif id_attr in data:
            instance = self.Meta.model.objects.get(**{id_attr: data[id_attr]})
            serializer = type(self)(
                instance,
                data=data,
                partial=True,
                context=self.context,
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()
        else:
            serializer = type(self)(
                data=data,
                partial=False,
                context=self.context,
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        try:
            request_method = getattr(
                self.context['view'].request,
                'method',
                '',
            )
        except BaseException:
            request_method = None

        if all((
            isinstance(self.parent, BulkListSerializer),
            id_attr,
            request_method in ('PUT', 'PATCH'),
        )):
            id_field = self.fields[id_attr]
            id_value = id_field.get_value(data)

            ret[id_attr] = id_value

        return ret

    def create(self, validated_data):
        data = {}
        many_to_many_fields = {}
        for field in self.fields:
            if field in validated_data:
                if isinstance(
                    self.fields[field],
                    (serializers.ListSerializer, serializers.ManyRelatedField),
                ):
                    many_to_many_fields[field] = validated_data[field]
                else:
                    data[field] = validated_data[field]
        instance = self.Meta.model.objects.create(**data)
        for field in many_to_many_fields:
            getattr(instance, field).set(many_to_many_fields[field])
        instance.save()
        return instance

    def update(self, instance, validated_data):
        for field in self.fields:
            if isinstance(
                self.fields[field],
                (serializers.Serializer, serializers.ListSerializer),
            ):
                v = validated_data
                path = []
                ins = instance
                source = self.fields[field].source
                if field in v:
                    while '.' in source:
                        path.append((source.partition('.')[0], v))
                        v = v[source.partition('.')[0]]
                        ins = getattr(ins, source.partition('.')[0])
                        source = source.partition('.')[2]
                    if isinstance(
                        ins._meta.get_field(field),
                        (ManyToManyField, ManyToManyRel, ManyToOneRel),
                    ):
                        getattr(ins, field).set(v[field])
                    else:
                        setattr(ins, field, v[field])
                    v.pop(field)
                    ins.save()
                    for field, data in reversed(path):
                        if len(data[field]) == 0:
                            del data[field]

        return super(BaseModelSerializer, self).update(
            instance,
            validated_data,
        )
