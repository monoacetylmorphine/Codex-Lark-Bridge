"""Validate resource bytes and untrusted metadata without network or disk I/O."""
from __future__ import annotations
import io, pytest
import zipfile, pytest
from pathlib import Path
from unittest.mock import Mock
from codex_bridge.middleware.resource_validator import ResourceValidationError, read_resource_bytes, validate_downloaded_resource, OFFICE_TYPES, MAX_OFFICE_METADATA_BYTES
from codex_bridge.configs.loader import CONFIG

def office_bytes(extension, *, main_type=None, target='custom/main.xml', overrides=None):
    output = io.BytesIO()
    parts = {'[Content_Types].xml': f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/{target}" ContentType="{main_type or OFFICE_TYPES[extension]}"/></Types>', '_rels/.rels': f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{target}"/></Relationships>', target: '<document/>'}
    parts.update(overrides or {})
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()

class ResourceValidatorTests:

    def validate(self, data=b'\x89PNG\r\n\x1a\n', **kwargs):
        options = dict(resource_key='image-key', resource_type='file', file_name=None)
        options.update(kwargs)
        return validate_downloaded_resource(data=data, **options)

    def test_supported_signatures_supply_canonical_extensions(self) -> None:
        cases = [(b'\xff\xd8\xff', 'jpeg', '.jpg'), (b'\x89PNG\r\n\x1a\n', 'png', '.png'), (b'GIF87a', 'gif', '.gif'), (b'GIF89a', 'gif', '.gif'), (b'RIFF1234WEBP', 'webp', '.webp'), (b'BM', 'bmp', '.bmp'), (b'%PDF-1.7', 'pdf', '.pdf'), (b'0000ftypisom', 'mp4', '.mp4'), (b'PK\x03\x04', 'zip', '.zip'), (b'PK\x05\x06', 'zip', '.zip'), (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', 'ole', '.bin'), (b'OggS', 'ogg', '.ogg'), (b'RIFF1234WAVE', 'wav', '.wav'), (b'ID3', 'mp3', '.mp3'), (b'\xff\xe0', 'mp3', '.mp3')]
        for data, format_name, extension in cases:
            result = self.validate(data)
            assert result.data == data
            assert result.detected_format == format_name
            assert result.extension == extension
            assert result.file_name == 'image-key' + extension

    def test_signature_repairs_misleading_extension(self) -> None:
        result = self.validate(file_name='photo-é.exe')
        assert result.file_name == 'photo-é.png'

    def test_matching_office_and_jpeg_extensions_are_preserved(self) -> None:
        for suffix, data in [(s, office_bytes(s)) for s in ('.docx', '.xlsx', '.pptx')] + [('.doc', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'), ('.jpeg', b'\xff\xd8\xff')]:
            assert self.validate(data, file_name='report' + suffix).extension == suffix

    def test_headers_are_case_insensitive_and_mime_parameters_are_removed(self) -> None:
        result = self.validate(headers={'CONTENT-TYPE': ' Image/PNG ; charset=binary', 'Content-Length': 8})
        assert result.mime_type == 'image/png'

    def test_content_length_must_be_valid_and_complete(self) -> None:
        for value in ('bad', '7', '9', '-1', '0'):
            with pytest.raises(ResourceValidationError):
                self.validate(headers={'Content-Length': value})

    def test_mime_conflict_and_nonimage_payload_are_rejected(self) -> None:
        for options in ({'headers': {'Content-Type': 'application/pdf'}}, {'data': b'%PDF-1.7', 'resource_type': 'image'}):
            with pytest.raises(ResourceValidationError):
                self.validate(**options)

    def test_generic_mime_allows_signature_detection(self) -> None:
        for mime in ('', 'application/octet-stream', 'binary/octet-stream'):
            assert self.validate(headers={'Content-Type': mime}).detected_format == 'png'

    def test_audio_container_metadata_selects_profile(self) -> None:
        for data, mime, name, expected in [(b'OggS', 'audio/opus', None, 'opus'), (b'0000ftypisom', 'audio/mp4', None, 'm4a'), (b'0000ftypisom', 'video/mp4', 'voice.m4a', 'm4a')]:
            assert self.validate(data, file_name=name, headers={'Content-Type': mime}).detected_format == expected

    def test_json_and_text_validate_actual_content(self) -> None:
        for data, name, expected in [(b'\xef\xbb\xbf{"ok":true}', 'a.json', 'json'), ('Hello, café\n'.encode(), 'a.txt', 'text')]:
            assert self.validate(data, file_name=name).detected_format == expected
        for data, name in [(b'{bad', 'a.json'), (b'\xfe', 'a.json'), (b'a\x00b', 'a.txt'), (b'\xfe', 'a.txt')]:
            with pytest.raises(ResourceValidationError):
                self.validate(data, file_name=name)

    def test_known_binary_claim_requires_signature(self) -> None:
        for suffix in ('.png', '.pdf', '.docx', '.mp4', '.wav', '.mp3'):
            with pytest.raises(ResourceValidationError):
                self.validate(b'renamed payload', file_name='fake' + suffix)

    def test_unknown_file_extension_is_preserved_for_assembly_filter(self) -> None:
        result = self.validate(b'custom bytes', file_name='data.custom')
        assert (result.detected_format, result.extension) == ('custom', '.custom')

    def test_invalid_type_empty_and_unidentified_payloads_are_rejected(self) -> None:
        for options in ({'resource_type': 'video'}, {'data': b''}, {'data': 'text'}, {'data': b'unknown'}, {'data': b'RIFF1234'}, {'data': b'\xff'}):
            with pytest.raises(ResourceValidationError):
                self.validate(**options)

    def test_filenames_cannot_escape_destination(self) -> None:
        for name in ('../../photo-é.png', '/tmp/a b.png', '..\\..\\photo-é.png', '...', None):
            result = self.validate(file_name=name, resource_key='../../unsafe/key')
            assert Path(result.file_name).name == result.file_name
            assert '\\' not in result.file_name
            assert not result.file_name.startswith('.')
            assert result.extension == '.png'
        assert self.validate(resource_key='.../').file_name == CONFIG['resources']['fileName']['fallbackStem'] + '.png'

    def test_bytesio_is_read_in_full_even_when_cursor_is_at_end(self) -> None:
        stream = io.BytesIO(b'image')
        stream.seek(0, io.SEEK_END)
        assert read_resource_bytes(stream) == b'image'

    def test_regular_stream_and_nonbinary_response(self) -> None:
        stream = Mock(spec=['read'], read=Mock(return_value=b'file'))
        assert read_resource_bytes(stream) == b'file'
        stream.read.assert_called_once_with()
        with pytest.raises(ResourceValidationError):
            read_resource_bytes(io.StringIO('not binary'))

    def test_text_extensions_survive_generic_mime_and_reject_binary_content(self) -> None:
        for suffix in ('.md', '.csv', '.jsonl', '.py', '.js', '.ts', '.html', '.xml', '.yml', '.yaml', '.toml', '.sh'):
            for mime in ('text/plain', 'application/octet-stream'):
                result = self.validate('Example\n'.encode(), file_name='sample' + suffix, headers={'content-type': mime})
                assert result.file_name == 'sample' + suffix
                for payload in (b'\x00binary', b'\xffbinary', b'\x89PNG\r\n\x1a\n'):
                    with pytest.raises(ResourceValidationError):
                        self.validate(payload, file_name='sample' + suffix, headers={'content-type': mime})

    def test_json_plain_mime_cannot_bypass_validation_and_jsonl_is_text(self) -> None:
        result = self.validate(b'{"a":1}', file_name='data.json', headers={'content-type': 'text/plain'})
        assert result.file_name == 'data.json'
        with pytest.raises(ResourceValidationError):
            self.validate(b'{bad', file_name='data.json', headers={'content-type': 'text/plain'})
        result = self.validate(b'{"a":1}\n{"b":2}\n', file_name='data.jsonl', headers={'content-type': 'application/x-ndjson'})
        assert result.extension == '.jsonl'

    def test_office_rejects_fake_zip_missing_parts_wrong_type_and_external_target(self) -> None:
        samples = [b'PK\x03\x04fake', office_bytes('.xlsx'), office_bytes('.docx', overrides={'[Content_Types].xml': '<Types/>'}), office_bytes('.docx', target='../outside.xml'), office_bytes('.docx', overrides={'_rels/.rels': '<Relationships/>'}), office_bytes('.docx', overrides={'custom/main.xml': ''})]
        for payload in samples:
            with pytest.raises(ResourceValidationError):
                self.validate(payload, file_name='report.docx')
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, 'w') as archive:
            archive.writestr('ordinary.txt', 'not office')
        with pytest.raises(ResourceValidationError):
            self.validate(empty.getvalue(), file_name='report.docx')

    def test_office_metadata_limits_and_entities_are_rejected(self) -> None:
        for metadata in (' ' * (MAX_OFFICE_METADATA_BYTES + 1), '<!DOCTYPE x [<!ENTITY e "expanded">]><x>&e;</x>'):
            with pytest.raises(ResourceValidationError):
                self.validate(office_bytes('.docx', overrides={'[Content_Types].xml': metadata}), file_name='report.docx')

    def test_office_default_content_types_and_override_precedence(self) -> None:
        for extension, expected_type in OFFICE_TYPES.items():
            for part_extension in ('xml', 'XML'):
                target = 'custom/main.' + part_extension
                default = f'<Default Extension="xml" ContentType="{expected_type}"/>'
                override = f'<Override PartName="/{target}" ContentType="{expected_type}"/>'
                wrong_override = f'<Override PartName="/{target}" ContentType="application/xml"/>'
                cases = [('default', default, True), ('override_wins', '<Default Extension="xml" ContentType="application/xml"/>' + override, True), ('wrong_override', default + wrong_override, False), ('missing_override_type', default + f'<Override PartName="/{target}"/>', False), ('duplicate_override', default + override + override, False), ('duplicate_default', default + default, False), ('wrong_default', '<Default Extension="xml" ContentType="application/xml"/>', False), ('unmatched_default', f'<Default Extension="bin" ContentType="{expected_type}"/>', False), ('missing_default_type', '<Default Extension="xml"/>', False), ('missing_declaration', '', False)]
                for label, declarations, accepted in cases:
                    payload = office_bytes(extension, target=target, overrides={'[Content_Types].xml': '\ufeff<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' + declarations + '</Types>', '_rels/.rels': f'\ufeff<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="/{target}"/></Relationships>'})
                    if accepted:
                        result = self.validate(payload, file_name='report' + extension)
                        assert result.extension == extension
                        assert result.data == payload
                    else:
                        with pytest.raises(ResourceValidationError, match='Office main-document type does not match its extension'):
                            self.validate(payload, file_name='report' + extension)

    def test_office_mime_supplies_extension_without_filename(self) -> None:
        payload = office_bytes('.docx')
        result = self.validate(payload, headers={'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'})
        assert result.extension == '.docx'
        with pytest.raises(ResourceValidationError):
            self.validate(payload, file_name='report.xlsx', headers={'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'})
