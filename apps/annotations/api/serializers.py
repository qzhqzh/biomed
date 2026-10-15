from rest_framework import serializers


class AnnotationRequestSerializer(serializers.Serializer):
    variants = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=500),
        allow_empty=False,
        help_text="Variant strings, e.g. '17:43092951 G>A'",
    )
    assembly = serializers.ChoiceField(
        choices=["GRCh37", "GRCh38"],
        default="GRCh37",
    )
    engine = serializers.ChoiceField(
        choices=["vep", "annovar", "both"],
        default="vep",
    )


class JobCreateSerializer(serializers.Serializer):
    variants = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=500),
        allow_empty=False,
    )
    assembly = serializers.ChoiceField(
        choices=["GRCh37", "GRCh38"],
        default="GRCh37",
    )
    engines = serializers.ListField(
        child=serializers.ChoiceField(choices=["vep", "annovar"]),
        allow_empty=False,
        help_text="One or more engines to run on the same variants/assembly",
    )

    def validate_engines(self, value):
        seen: list[str] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        if not seen:
            raise serializers.ValidationError("至少选择一种引擎")
        return seen


class TranscriptHitSerializer(serializers.Serializer):
    gene = serializers.CharField(allow_null=True, required=False)
    feature = serializers.CharField(allow_null=True, required=False)
    cdot = serializers.CharField(allow_null=True, required=False)
    protein = serializers.CharField(allow_null=True, required=False)
    preferred = serializers.BooleanField(required=False)
    mane = serializers.BooleanField(required=False)
    mane_status = serializers.CharField(allow_null=True, required=False)
    source_index = serializers.IntegerField(required=False)


class VariantResultSerializer(serializers.Serializer):
    input = serializers.CharField()
    allele = serializers.CharField(allow_null=True, required=False)
    gene = serializers.CharField(allow_null=True, required=False)
    feature = serializers.CharField(allow_null=True, required=False)
    consequence = serializers.CharField(allow_null=True, required=False)
    impact = serializers.CharField(allow_null=True, required=False)
    cdot = serializers.CharField(allow_null=True, required=False)
    protein = serializers.CharField(allow_null=True, required=False)
    biotype = serializers.CharField(allow_null=True, required=False)
    canonical = serializers.CharField(allow_null=True, required=False)
    engine = serializers.CharField(required=False)
    transcripts = TranscriptHitSerializer(many=True, required=False)
    details = serializers.DictField(required=False)


class AnnotationResponseSerializer(serializers.Serializer):
    knowledge_version = serializers.JSONField(required=False)
    assembly = serializers.CharField()
    engine = serializers.CharField()
    results = VariantResultSerializer(many=True)
