import struct
import os
import io
import tempfile
import shutil

from pyffi_ext.formats.dds import DdsFormat
from pyffi_ext.formats.ms2 import Ms2Format
# from pyffi_ext.formats.bani import BaniFormat
# from pyffi_ext.formats.ovl import OvlFormat
from pyffi_ext.formats.fgm import FgmFormat
from pyffi_ext.formats.materialcollection import MaterialcollectionFormat
# from pyffi_ext.formats.assetpkg import AssetpkgFormat

from modules import extract
from util import texconv, imarray


def split_path(fp):
	in_dir, name_ext = os.path.split(fp)
	name, ext = os.path.splitext(name_ext)
	ext = ext.lower()
	return name_ext, name, ext


def inject(ovl_data, file_paths, show_dds):

	# write modified version to tmp dir
	tmp_dir = tempfile.mkdtemp("-cobra-png")

	dupecheck = []
	for file_path in file_paths:
		name_ext, name, ext = split_path(file_path)
		print("Injecting", name_ext)
		# check for separated array tiles & flipped channels
		if ext == ".png":
			out_path = imarray.inject_wrapper(file_path, dupecheck, tmp_dir)
			# skip dupes
			if not out_path:
				print("Skipping injection of", file_path)
				continue
			# update the file path to the temp file with flipped channels or rebuilt array
			file_path = out_path
			name_ext, name, ext = split_path(file_path)
		# image files are stored as tex files in the archive
		if ext in (".dds", ".png"):
			name_ext = name+".tex"
		elif ext == ".matcol":
			name_ext = name+".materialcollection"
		# find the sizedstr entry that refers to this file
		sized_str_entry = ovl_data.get_sized_str_entry(name_ext)
		# do the actual injection, varies per file type
		if ext == ".mdl2":
			load_mdl2(ovl_data, file_path, sized_str_entry)
		if ext == ".fgm":
			load_fgm(ovl_data, file_path, sized_str_entry)
		elif ext == ".png":
			load_png(ovl_data, file_path, sized_str_entry, show_dds)
		elif ext == ".dds":
			load_dds(ovl_data, file_path, sized_str_entry)
		elif ext == ".txt":
			load_txt(ovl_data, file_path, sized_str_entry)
		elif ext == ".xmlconfig":
			load_xmlconfig(ovl_data, file_path, sized_str_entry)
		elif ext == ".fdb":
			load_fdb(ovl_data, file_path, sized_str_entry, name)
		elif ext == ".matcol":
			load_materialcollection(ovl_data, file_path, sized_str_entry)
		elif ext == ".lua":
			load_lua(ovl_data, file_path, sized_str_entry)
		elif ext == ".assetpkg":
			load_assetpkg(ovl_data, file_path, sized_str_entry)
		  
	shutil.rmtree(tmp_dir)



def to_bytes(inst, data):
	"""helper that returns the bytes representation of a pyffi struct"""
	if isinstance(inst, bytes):
		return inst
	# zero terminated strings show up as strings
	if isinstance(inst, str):
		return inst.encode() + b"\x00"
	with io.BytesIO() as frag_writer:
		inst.write(frag_writer, data=data)
		return frag_writer.getvalue()


def load_txt(ovl_data, txt_file_path, txt_sized_str_entry):

	archive = ovl_data.archives[0]
	with open(txt_file_path, 'rb') as stream:
		raw_txt_bytes = stream.read()
		data = struct.pack("<I", len(raw_txt_bytes)) + raw_txt_bytes
		# make sure all are updated, and pad to 8 bytes
		txt_sized_str_entry.pointers[0].update_data(data, update_copies=True, pad_to=8)


def load_xmlconfig(ovl_data, xml_file_path, xml_sized_str_entry):
	archive = ovl_data.archives[0]
	with open(xml_file_path, 'rb') as stream:
		# add zero terminator
		data = stream.read() + b"\x00"
		# make sure all are updated, and pad to 8 bytes
		xml_sized_str_entry.fragments[0].pointers[1].update_data(data, update_copies=True, pad_to=8)


def load_png(ovl_data, png_file_path, tex_sized_str_entry, show_dds):
	# convert the png into a dds, then inject that

	archive = ovl_data.archives[0]
	header_3_0, header_3_1, header_7 = extract.get_tex_structs(archive, tex_sized_str_entry)
	dds_compression_type = extract.get_compression_type(header_3_0)
	# texconv works without prefix
	compression = dds_compression_type.replace("DXGI_FORMAT_","")

	dds_file_path = texconv.png_to_dds( png_file_path, header_7.height*header_7.array_size, show_dds, codec = compression, mips = header_7.num_mips)
	# inject the dds generated by texconv
	load_dds(ovl_data, dds_file_path, tex_sized_str_entry)
	# remove the temp file if desired
	texconv.clear_tmp(dds_file_path, show_dds)


def ensure_size_match(name, dds_header, tex_header, comp):
	"""Check that DDS files have the same basic size"""
	dds_h = dds_header.height
	dds_w = dds_header.width
	dds_d = dds_header.depth
	dds_a = dds_header.dx_10.array_size

	tex_h = tex_header.height
	tex_w = extract.align_to(tex_header.width, comp)
	tex_d = tex_header.depth
	tex_a = tex_header.array_size

	if dds_h * dds_w * dds_d * dds_a != tex_h * tex_w * tex_d * tex_a:
		raise AttributeError(f"Dimensions do not match for {name}!\n\n"
							 f"Dimensions: height x width x depth [array size]\n"
							 f"OVL Texture: {tex_h} x {tex_w} x {tex_d} [{tex_a}]\n"
							 f"Injected texture: {dds_h} x {dds_w} x {dds_d} [{dds_a}]\n\n"
							 f"Make the external texture's dimensions match the OVL texture and try again!" )


def pack_mips(stream, header, num_mips):
	"""From a standard DDS stream, pack the lower mip levels into one image and pad with empty bytes"""
	print("\nPacking mips")

	normal_levels = []
	packed_levels = []

	# get compression type
	dds_types = {}
	dds_enum = DdsFormat.DxgiFormat
	for k, v in zip(dds_enum._enumkeys, dds_enum._enumvalues):
		dds_types[v] = k
	comp = dds_types[header.dx_10.dxgi_format]

	# get bpp from compression type
	if "BC1" in comp or "BC4" in comp:
		pixels_per_byte = 2
		empty_block = bytes.fromhex("00 00 00 00 00 00 00 00")
	else:
		pixels_per_byte = 1
		empty_block = bytes.fromhex("00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")

	h = header.height
	w = header.width
	mip_i = 0

	# print("\nstandard mips")
	# the last normal mip is 64x64
	# no, wrong, check herrera pbasecolor
	# start packing when one line of the mip == 128 bytes
	while w // pixels_per_byte > 32:
		# print(mip_i, h, w)
		num_pixels = h * w * header.dx_10.array_size
		num_bytes = num_pixels // pixels_per_byte
		address = stream.tell()
		# print(address, num_pixels, num_bytes)
		normal_levels.append( (h, w, stream.read(num_bytes)) )
		h //= 2
		w //= 2
		mip_i += 1

		# no packing at all, just grab desired mips and done
		if num_mips == mip_i:
			print(f"Info: MIP packing is not needed. Grabbing MIP level {mip_i} directly.")
			return b"".join( x[2] for x in normal_levels )

	# print("\npacked mips")
	# compression blocks are 4x4 pixels
	while h > 2 and w > 2:
		# print(mip_i, h, w)
		num_pixels = h * w * header.dx_10.array_size
		num_bytes = num_pixels // pixels_per_byte
		address = stream.tell()
		# print(address, num_pixels, num_bytes)
		packed_levels.append( (h, w, stream.read(num_bytes)) )
		h //= 2
		w //= 2
		mip_i += 1

	with io.BytesIO() as packed_writer:
		# 1 byte per pixel = 64 px
		# 0.5 bytes per pixel = 128 px
		total_width = 64 * pixels_per_byte
		# pack the last mips into one image
		for i, (height, width, level_bytes) in enumerate(packed_levels):

			# write horizontal lines

			# get count of h slices, 1 block is 4x4 px
			num_slices_y = height // 4
			num_pad_x = (total_width - width) // 4
			bytes_per_line = len(level_bytes) // num_slices_y

			# write the bytes for this line from the mip bytes
			for slice_i in range(num_slices_y):
				# get the bytes that represent the blocks of this line
				sl = level_bytes[ slice_i*bytes_per_line : (slice_i+1)*bytes_per_line ]
				packed_writer.write( sl )
				# fill the line with padding blocks
				for k in range(num_pad_x):
					packed_writer.write( empty_block )

		# weird stuff at the end
		for j in range(2):
			# empty line
			for k in range(64 // 4):
				packed_writer.write( empty_block )

			# write 4x4 lod
			packed_writer.write( level_bytes )

			# pad line
			for k in range(60 // 4):
				packed_writer.write( empty_block )
		# empty line
		for k in range(64 // 4):
			packed_writer.write( empty_block )

		# still gotta add one more lod here
		if pixels_per_byte == 2:
			# empty line
			for k in range(16):
				packed_writer.write( empty_block )
			# write 4x4 lod
			packed_writer.write( level_bytes )
			# padding
			for k in range(63):
				packed_writer.write( empty_block )

		packed_mip_bytes = packed_writer.getvalue()

	out_mips = [ x[2] for x in normal_levels ]
	out_mips.append(packed_mip_bytes)

	# get final merged output bytes
	return b"".join( out_mips )


def load_dds(ovl_data, dds_file_path, tex_sized_str_entry):

	# read archive tex header to make sure we have the right mip count
	# even when users import DDS with mips when it should have none
	archive = ovl_data.archives[0]
	header_3_0, header_3_1, header_7 = extract.get_tex_structs(archive, tex_sized_str_entry)

	# load dds
	with open(dds_file_path, 'rb') as stream:
		version = DdsFormat.version_number("DX10")
		dds_data = DdsFormat.Data(version=version)
		# no stream, but data version even though that's broken
		header = DdsFormat.Header(stream, dds_data)
		header.read(stream, dds_data)
		comp = extract.get_compression_type(header_3_0)
		ensure_size_match(os.path.basename(dds_file_path), header, header_7, comp)
		# print(header)
		out_bytes = pack_mips(stream, header, header_7.num_mips)
		# with open(dds_file_path+"dump.dds", 'wb') as stream:
		# 	header.write(stream, dds_data)
		# 	stream.write(out_bytes)

	sum_of_buffers = sum(buffer.size for buffer in tex_sized_str_entry.data_entry.buffers)
	if len(out_bytes) != sum_of_buffers:
		print(f"Packing of MipMaps failed. OVL expects {sum_of_buffers} bytes, but packing generated {len(out_bytes)} bytes." )

	with io.BytesIO(out_bytes) as reader:
		for buffer in tex_sized_str_entry.data_entry.buffers:
			dds_buff = reader.read(buffer.size)
			if len(dds_buff) < buffer.size:
				print(f"Last {buffer.size - len(dds_buff)} bytes of DDS buffer are not overwritten!")
				dds_buff = dds_buff + buffer.data[len(dds_buff):]
			buffer.update_data(dds_buff)


def load_mdl2(ovl_data, mdl2_file_path, mdl2_sized_str_entry):
	# read mdl2, find ms2
	# inject ms2 buffers
	# update ms2 + mdl2 fragments

	# these fragments will be overwritten
	model_data_frags = []
	buff_datas = []
	mdl2_data = Ms2Format.Data()
	with open(mdl2_file_path, "rb") as mdl2_stream:
		mdl2_data.inspect(mdl2_stream)
		ms2_name = mdl2_data.mdl2_header.name.decode()
		for modeldata in mdl2_data.mdl2_header.models:
			model_data_frags.append( to_bytes(modeldata, mdl2_data) )
		lodinfo = to_bytes(mdl2_data.mdl2_header.lods, mdl2_data)

	# get ms2 buffers
	ms2_dir = os.path.dirname(mdl2_file_path)
	ms2_path = os.path.join(ms2_dir, ms2_name)
	with open(ms2_path, "rb") as ms2_stream:
		ms2_header = Ms2Format.Ms2InfoHeader()
		ms2_header.read(ms2_stream, data=mdl2_data)

		# get buffer info
		buffer_info = to_bytes(ms2_header.buffer_info, mdl2_data)

		# get buffer 0
		buff_datas.append( to_bytes(ms2_header.name_hashes, mdl2_data) + to_bytes(ms2_header.names, mdl2_data) )
		# get buffer 1
		buff_datas.append( ms2_stream.read(ms2_header.bone_info_size) )
		# get buffer 2
		buff_datas.append( ms2_stream.read() )

	# get ms2 sized str entry
	ms2_sized_str_entry = ovl_data.get_sized_str_entry(ms2_name)
	ms2_sized_str_entry.data_entry.update_data(buff_datas)

	# the actual injection

	# overwrite mdl2 modeldata frags
	for frag, frag_data in zip(mdl2_sized_str_entry.model_data_frags, model_data_frags):
		frag.pointers[0].update_data(frag_data, update_copies=True)

	# overwrite mdl2 lodinfo frag
	mdl2_sized_str_entry.fragments[1].pointers[1].update_data(lodinfo, update_copies=True)

	# overwrite ms2 buffer info frag
	buffer_info_frag = ms2_sized_str_entry.fragments[0]
	buffer_info_frag.pointers[1].update_data(buffer_info, update_copies=True)


def load_fgm(ovl_data, fgm_file_path, fgm_sized_str_entry):

	fgm_data = FgmFormat.Data()
	# open file for binary reading
	with open(fgm_file_path, "rb") as stream:
		fgm_data.read(stream, fgm_data, file=fgm_file_path)

		sizedstr_bytes = to_bytes(fgm_data.fgm_header.fgm_info, fgm_data) + to_bytes(fgm_data.fgm_header.two_frags_pad, fgm_data)

		# todo - move texpad into fragment padding?
		textures_bytes = to_bytes(fgm_data.fgm_header.textures, fgm_data) + to_bytes(fgm_data.fgm_header.texpad, fgm_data)
		attributes_bytes = to_bytes(fgm_data.fgm_header.attributes, fgm_data)

		# read the other datas
		stream.seek(fgm_data.eoh)
		zeros_bytes = stream.read(fgm_data.fgm_header.zeros_size)
		data_bytes = stream.read(fgm_data.fgm_header.data_lib_size)
		buffer_bytes = stream.read()

	# the actual injection
	fgm_sized_str_entry.data_entry.update_data( (buffer_bytes,) )
	fgm_sized_str_entry.pointers[0].update_data(sizedstr_bytes, update_copies=True)

	if len(fgm_sized_str_entry.fragments) == 4:
		datas = (textures_bytes, attributes_bytes, zeros_bytes, data_bytes)
	# fgms without zeros
	elif len(fgm_sized_str_entry.fragments) == 3:
		datas = (textures_bytes, attributes_bytes, data_bytes)
	# fgms for variants
	elif len(fgm_sized_str_entry.fragments) == 2:
		datas = (attributes_bytes, data_bytes)
	else:
		raise AttributeError("Unexpected fgm frag count")

	# inject fragment datas
	for frag, data in zip(fgm_sized_str_entry.fragments, datas):
		frag.pointers[1].update_data(data, update_copies=True)


def update_matcol_pointers(pointers, new_names):
	# it looks like fragments are not reused here, and not even pointers are
	# but as they point to the same address the writer treats them as same
	# so the pointer map has to be updated for the involved header entries
	# also the copies list has to be adjusted

	# so this is a hack that only considers one entry for each union of pointers
	# map doffset to tuple of pointer and new data
	dic = {}
	for p, n in zip(pointers, new_names):
		dic[p.data_offset] = (p, n.encode() + b"\x00")
	sorted_keys = list(sorted(dic))
	# print(sorted_keys)
	print("Names in ovl order:", list(dic[k][1] for k in sorted_keys))
	sum = 0
	for k in sorted_keys:
		p, d = dic[k]
		sum += len(d)
		for pc in p.copies:
			pc.data = d
			pc.padding = b""
	pad_to = 64
	mod = sum % pad_to
	if mod:
		padding = b"\x00" * (pad_to-mod)
	else:
		padding = b""
	for pc in p.copies:
		pc.padding = padding


def load_materialcollection(ovl_data, matcol_file_path, sized_str_entry):
	matcol_data = MaterialcollectionFormat.Data()
	# open file for binary reading
	with open(matcol_file_path, "rb") as stream:
		matcol_data.read(stream)
		# print(matcol_data.header)

		if sized_str_entry.has_texture_list_frag:
			pointers = [tex_frag.pointers[1] for tex_frag in sized_str_entry.tex_frags]
			new_names = [n for t in matcol_data.header.texture_wrapper.textures for n in (t.fgm_name, t.texture_suffix, t.texture_type)]
		else:
			pointers = []
			new_names = []

		if sized_str_entry.is_variant:
			for (m0,), variant in zip(sized_str_entry.mat_frags, matcol_data.header.variant_wrapper.materials):
				# print(layer.name)
				pointers.append(m0.pointers[1])
				new_names.append(variant)
		elif sized_str_entry.is_layered:
			for (m0, info, attrib), layer in zip(sized_str_entry.mat_frags, matcol_data.header.layered_wrapper.layers):
				# print(layer.name)
				pointers.append(m0.pointers[1])
				new_names.append(layer.name)
				for frag, wrapper in zip(info.children, layer.infos):
					frag.pointers[0].update_data( to_bytes(wrapper.info, matcol_data), update_copies=True )
					frag.pointers[1].update_data( to_bytes(wrapper.name, matcol_data), update_copies=True )
					pointers.append(frag.pointers[1])
					new_names.append(wrapper.name)
				for frag, wrapper in zip(attrib.children, layer.attribs):
					frag.pointers[0].update_data( to_bytes(wrapper.attrib, matcol_data), update_copies=True )
					frag.pointers[1].update_data( to_bytes(wrapper.name, matcol_data), update_copies=True )
					pointers.append(frag.pointers[1])
					new_names.append(wrapper.name)

		update_matcol_pointers(pointers, new_names)


def load_fdb(ovl_data, fdb_file_path, fdb_sized_str_entry, fdb_name):
	# read fdb
	# inject fdb buffers
	# update sized string

	with open(fdb_file_path, "rb") as fdb_stream:
		# load the new buffers
		buffer1_bytes = fdb_stream.read()
		buffer0_bytes = fdb_name.encode()
		# update the buffers
		fdb_sized_str_entry.data_entry.update_data( (buffer0_bytes, buffer1_bytes) )
		# update the sizedstring entry
		data = struct.pack("<8I", len(buffer1_bytes), 0, 0, 0, 0, 0, 0, 0)
		fdb_sized_str_entry.pointers[0].update_data(data, update_copies=True)

def load_assetpkg(ovl_data, assetpkg_file_path, sized_str_entry):
	with open(assetpkg_file_path, "rb") as stream:
		b = stream.read()
		sized_str_entry.fragments[0].pointers[1].update_data( b + b"\x00", update_copies=True, pad_to=64)
        
def load_lua(ovl_data, lua_file_path, lua_sized_str_entry):
	# read lua
	# inject lua buffer
	# update sized string
	#IMPORTANT: all meta data of the lua except the sized str entries lua size value seems to just be meta data, can be zeroed
	with open(lua_file_path, "rb") as lua_stream:
		# load the new buffer
		buffer_bytes = lua_stream.read()
		# update the buffer
		lua_sized_str_entry.data_entry.update_data( (buffer_bytes,))
		# update the sizedstring entry
	with open(lua_file_path+"meta","rb") as luameta_stream:
		string_data = luameta_stream.read(16)
		print(string_data) #4 uints: size,unknown,hash,zero. only size is used by game.
		frag0_data0 = luameta_stream.read(8)
		print(frag0_data0)
		frag0_data1 = luameta_stream.read(lua_sized_str_entry.fragments[0].pointers[1].data_size)
		print(frag0_data1)
		frag1_data0 = luameta_stream.read(24)
		print(frag1_data0)
		frag1_data1 = luameta_stream.read(lua_sized_str_entry.fragments[1].pointers[1].data_size)
		print(frag1_data1)
		lua_sized_str_entry.pointers[0].update_data(string_data, update_copies=True)
		lua_sized_str_entry.fragments[0].pointers[1].update_data(frag0_data1, update_copies=True)
		lua_sized_str_entry.fragments[1].pointers[1].update_data(frag1_data1, update_copies=True)  
