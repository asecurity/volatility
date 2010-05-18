# Volatility
#
# Authors:
# {npetroni,awalters}@4tphi.net (Nick Petroni and AAron Walters)
# Jesse Kornblum
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details. 
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA 
#

""" This is Jesse Kornblum's patch to clean up the standard AS's.
"""
import struct
import standard
import xml.etree.cElementTree as etree
import volatility.addrspace as addrspace
import volatility.obj as obj 
import volatility.conf
config = volatility.conf.ConfObject()
import volatility.debug as debug #pylint: disable-msg=W0611

BLOCKSIZE = 1024 * 1024 * 10

config.add_option("CACHE-DTB", action="store_false", default=True, 
                  help = "Cache virtual to physical mappings")


class JKIA32PagedMemoryBase(addrspace.BaseAddressSpace, standard.WritablePagedMemory):
    """ Standard x86 32 bit non PAE address space.
    
    Provides an address space for IA32 paged memory, aka the x86 
    architecture, without Physical Address Extensions (PAE). Allows
    callers to map virtual address to offsets in physical memory.

    Create a new IA32 address space without PAE to sit on top of 
    the base address space and a Directory Table Base (CR3 value)
    of 'dtb'. 
    
    If the 'cache' parameter is true, will cache the Page Directory Entries
    for extra performance. The cache option requires an additional 4KB of
    space.

    Comments in this class mostly come from the Intel(R) 64 and IA-32 
    Architectures Software Developer's Manual Volume 3A: System Programming 
    Guide, Part 1, revision 031, pages 4-8 to 4-15. This book is available
    for free at http://www.intel.com/products/processor/manuals/index.htm.
    Similar information is also available from Advanced Micro Devices (AMD) 
    at http://support.amd.com/us/Processor_TechDocs/24593.pdf.
    """
    order = 70
    cache = False
    pae = False
    paging_address_space = True
    name = 'Kernel AS'
    
    def __init__(self, base, dtb=0, astype = None, **kwargs):
        ## We allow users to disable us in favour of the old legacy
        ## modules.
        assert not config.USE_OLD_AS, "Module disabled"
        standard.WritablePagedMemory.__init__(self, base)
        addrspace.BaseAddressSpace.__init__(self, base, **kwargs)
        assert astype != 'physical', "User requested physical AS"

        ## We must be stacked on someone else:
        assert base, "No base Address Space"

        ## We can not stack on someone with a dtb
        try:
            assert not base.paging_address_space, "Can not stack over another paging address space"
        except AttributeError:
            pass

        self.dtb = dtb or config.DTB or self.load_dtb()
        self.base = base

        if not self.isValidKernelAS():
            raise Exception()

        # The caching code must be in a separate function to allow the
        # PAE code, which inherits us, to have its own code.
        self.cache = config.CACHE_DTB
        if self.cache:
            self._cache_values()

        # Reserved for future use
        #self.pagefile = config.PAGEFILE

    def isValidKernelAS(self):
        ## We have to have a valid PsLoadedModuleList
        # FIXME: !!!!! Remove Hardcoded HACK!!!!
        # TODO: BLS: Ugly hack!
        #assert self.is_valid_address(0x8055a420), "PsLoadedModuleList not valid Address"

        foundKernelAddresses = False
        for (offset,length) in self.get_available_pages(start=0x80000000):
            if (offset > 0x80000000):
                foundKernelAddresses = True
                break
        return foundKernelAddresses


    def _cache_values(self):
        '''
        We cache the Page Directory Entries to avoid having to 
        look them up later. There is a 0x1000 byte memory page
        holding the four byte PDE. 0x1000 / 4 = 0x400 entries
        '''
        buf = self.base.read(self.dtb, 0x1000)
        if buf is None:
            self.cache = False
        else:
            self.pde_cache = struct.unpack('<'+'L' * 0x400, buf)

    def render_xml(self):
        """Renders the Address Space as XML"""
        result = etree.Element('address_space', {'type': self.__class__.__name__,
                                                 'name': self.name})

        if self.base:
            if self.base.render_xml():
                result.append(self.base.render_xml())

        return result

    def load_dtb(self):
        try:
            ## Try to be lazy and see if someone else found dtb for
            ## us:
            return self.base.dtb
        except AttributeError:
            ## Ok so we need to find our dtb ourselves:
            dtb = self._find_dtb()
            if dtb:
                ## Make sure to save dtb for other AS's
                self.base.dtb = dtb
                return dtb

    def _find_dtb(self):
        offset = 0
        while 1:
            data = self.base.read(offset, BLOCKSIZE)
            found = 0
            if not data:
                break

            while 1:
                # This value is specific to x86 Windows XP and must
                # updated for other operating systems
                found = data.find("\x03\x00\x1b\x00", found+1)
                if found >= 0:
                    # (_type, _size) = unpack('=HH', data[found:found+4])
                    proc = obj.Object("_EPROCESS",
                                             offset = offset+found,
                                             vm=self.base)

                    if 'Idle' in proc.ImageFileName.v():
                        return proc.Pcb.DirectoryTableBase.v()
                else:
                    break

            offset += len(data)
        return None


    def entry_present(self, entry):
        '''
        Returns whether or not the 'P' (Present) flag is on 
        in the given entry
        '''
        return (entry & 1) == 1

    def page_size_flag(self, entry):
        '''
        Returns whether or not the 'PS' (Page Size) flag is on
        in the given entry
        '''
        return (entry & (1 << 7)) == (1 << 7)

    def pde_index(self, vaddr):
        ''' 
        Returns the Page Directory Entry Index number from the given
        virtual address. The index number is in bits 31:22.
        '''
        return vaddr >> 22

    def get_pde(self, vaddr):
        '''
        Return the Page Directory Entry for the given virtual address.
        If caching

        Bits 31:12 are from CR3
        Bits 11:2 are bits 31:22 of the linear address
        Bits 1:0 are 0.
        '''
        if self.cache:
            return self.pde_cache[self.pde_index(vaddr)]

        pde_addr = (self.dtb & 0xfffff000) | ((vaddr & 0xffc00000) >> 20)
        return self.read_long_phys(pde_addr)

    def get_pte(self, vaddr, pde_value):
        '''
        Return the Page Table Entry for the given virtual address and
        Page Directory Entry.

        Bits 31:12 are from the PDE
        Bits 11:2 are bits 21:12 of the linear address
        Bits 1:0 are 0
        '''
        pte_addr = (pde_value & 0xfffff000) | ((vaddr & 0x3ff000) >> 10)
        return self.read_long_phys(pte_addr)

    def get_phys_addr(self, vaddr, pte_value):
        '''
        Return the offset in a 4KB memory page from the given virtual
        address and Page Table Entry.

        Bits 31:12 are from the PTE
        Bits 11:0 are from the original linear address
        '''
        return (pte_value & 0xfffff000) | (vaddr & 0xfff)

    def get_four_meg_paddr(self, vaddr, pde_value):
        '''
        Bits 31:22 are bits 31:22 of the PDE
        Bits 21:0 are from the original linear address
        '''
        return  (pde_value & 0xffc00000) | (vaddr & 0x3fffff)


    def vtop(self, vaddr):
        '''
        Translates virtual addresses into physical offsets.
        The function should return either None (no valid mapping)
        or the offset in physical memory where the address maps.
        '''
        pde_value = self.get_pde(vaddr)
        if not self.entry_present(pde_value):
            # Add support for paged out PDE
            # (insert buffalo here!)
            return None

        if self.page_size_flag(pde_value):
            return self.get_four_meg_paddr(vaddr, pde_value)

        pte_value = self.get_pte(vaddr, pde_value)
        if not self.entry_present(pte_value):
            # Add support for paged out PTE
            return None

        return self.get_phys_addr(vaddr, pte_value)

    def __read_chunk(self, vaddr, length):
        """
        Read 'length' bytes from the virtual address 'vaddr'.
        If vaddr does not have a valid mapping, return None.

        This function should not be called from outside this class
        as it doesn't take page breaks into account. That is,
        the bytes at virtual addresses 0x1fff and 0x2000 are not
        guarenteed to be contigious. Calling functions are responsible
        for determining contiguious blocks.
        """
        paddr = self.vtop(vaddr)
        if paddr is None:
            return None

        if not self.base.is_valid_address(paddr):
            return None

        return self.base.read(paddr, length)


    def __read_bytes(self, vaddr, length, pad):
        """
        Read 'length' bytes from the virtual address 'vaddr'.
        The 'pad' parameter controls whether unavailable bytes 
        are padded with zeros.
        """
        vaddr, length = int(vaddr), int(length)

        ret = ''

        while length > 0:
            chunk_len = min(length, 0x1000 - (vaddr % 0x1000))

            buf = self.__read_chunk(vaddr, chunk_len)
            if buf is None:
                if pad:
                    buf = '\x00' * chunk_len
                else:
                    return ''

            ret    += buf
            vaddr  += chunk_len
            length -= chunk_len

        return ret


    def read(self, vaddr, length):
        '''
        Read and return 'length' bytes from the virtual address 'vaddr'. 
        If any part of that block is unavailable, return None.
        '''
        return self.__read_bytes(vaddr, length, pad = False)

    def zread(self, vaddr, length):
        '''
        Read and return 'length' bytes from the virtual address 'vaddr'. 
        If any part of that block is unavailable, pad it with zeros.
        '''
        return self.__read_bytes(vaddr, length, pad = True)

    def read_long_phys(self, addr):
        '''
        Returns an unsigned 32-bit integer from the address addr in
        physical memory. If unable to read from that location, returns None.
        '''

        string = self.base.read(addr, 4)
        if not string:
            return None
        (longval, ) =  struct.unpack('<L', string)
        return longval

    def is_valid_address(self, addr):
        '''
        Returns True if addr maps to a valid location in physical,
        otherwise False.
        '''
        if addr == None:
            return False
        try:    
            phyaddr = self.vtop(addr)
        except:
            return False

        if phyaddr == None:
            return False
        return self.base.is_valid_address(phyaddr)

    def get_available_pages(self, start=0):
        '''
        Return a list of lists of available memory pages.
        Each entry in the list is the starting virtual address 
        and the size of the memory page.
        '''
        page_list = []
        start_pde_ent = self.pde_index(start)
        # Pages that hold PDEs and PTEs are 0x1000 bytes each.
        # Each PDE and PTE is four bytes. Thus there are 0x1000 / 4 = 0x400
        # PDEs and PTEs we must test

        for pde in range(start_pde_ent, 0x400):
            vaddr = pde << 22
            pde_value = self.get_pde(vaddr)
            if not self.entry_present(pde_value):
                continue
            if self.page_size_flag(pde_value):
                yield (vaddr, 0x400000)
            else:
                tmp = vaddr
                for pte in range(0, 0x400):
                    vaddr = tmp | (pte << 12)
                    pte_value = self.get_pte(vaddr, pde_value)
                    if self.entry_present(pte_value):
                        yield (vaddr, 0x1000)


class JKIA32PagedMemory(JKIA32PagedMemoryBase):
    def isValidKernelAS(self):
        header = None
        try:
            header = self.base.header
        except:
            pass
        if header != None:
            assert header.PaeEnabled == 1
        return JKIA32PagedMemoryBase.isValidKernelAS(self)

              
          
class JKIA32PagedMemoryPae(JKIA32PagedMemoryBase):
    """ Standard x86 32 bit PAE address space.
    
    Provides an address space for IA32 paged memory, aka the x86 
    architecture, with Physical Address Extensions (PAE) enabled. Allows
    callers to map virtual address to offsets in physical memory.

    Comments in this class mostly come from the Intel(R) 64 and IA-32 
    Architectures Software Developer's Manual Volume 3A: System Programming 
    Guide, Part 1, revision 031, pages 4-15 to 4-23. This book is available
    for free at http://www.intel.com/products/processor/manuals/index.htm.
    Similar information is also available from Advanced Micro Devices (AMD) 
    at http://support.amd.com/us/Processor_TechDocs/24593.pdf.
    """
    order = 80        
    pae = True
    
    def isValidKernelAS(self):
        header = None
        try:
            header = self.base.header
        except:
            pass
        if header != None:
            assert header.PaeEnabled == 1
        return JKIA32PagedMemoryBase.isValidKernelAS(self)
        
    def _cache_values(self):
        buf = self.base.read(self.dtb, 0x20)
        if buf is None:
            self.cache = False
        else:
            self.pdpte_cache = struct.unpack('<'+'Q' * 4, buf)
        
    def pdpte_index(self, vaddr):
        '''
        Compute the Page Directory Pointer Table index using the
        virtual address.

        The index comes from bits 31:30 of the original linear address.
        '''
        return vaddr >> 30

    def get_pdpte(self, vaddr):
        '''
        Return the Page Directory Pointer Table Entry for the given
        virtual address. Uses the cache if available, otherwise:

        Bits 31:5 come from CR3
        Bits 4:3 come from bits 31:30 of the original linear address
        Bits 2:0 are all 0
        '''
        if self.cache:
            return self.pdpte_cache[self.pdpte_index(vaddr)]

        pdpte_addr = (self.dtb & 0xffffffe0) | ((vaddr & 0xc0000000) >> 27)
        return self.read_long_long_phys(pdpte_addr)

    def get_pde(self, vaddr, pdpte):
        '''
        Return the Page Directory Entry for the given virtual address
        and Page Directory Pointer Table Entry.

        Bits 51:12 are from the PDPTE
        Bits 11:3 are bits 29:21 of the linear address
        Bits 2:0 are 0
        '''
        pde_addr = (pdpte & 0xffffffffff000) | ((vaddr & 0x3fe00000) >> 18)
        return self.read_long_long_phys(pde_addr)
    

    def get_two_meg_paddr(self, vaddr, pde):
        '''
        Return the offset in a 2MB memory page from the given virtual
        address and Page Directory Entry.

        Bits 51:21 are from the PDE
        Bits 20:0 are from the original linear address
        '''
        return (pde & 0xfffffffe00000) | (vaddr & 0x1fffff)

    def get_pte(self, vaddr, pde):
        '''
        Return the Page Table Entry for the given virtual address
        and Page Directory Entry.

        Bits 51:12 are from the PDE
        Bits 11:3 are bits 20:12 of the original linear address
        Bits 2:0 are 0
        '''
        pte_addr = (pde & 0xffffffffff000) | ((vaddr & 0x1ff000) >> 9)
        return self.read_long_long_phys(pte_addr)

    def get_phys_addr(self, vaddr, pte):
        '''
        Return the offset in a 4KB memory page from the given virtual
        address and Page Table Entry.

        Bits 51:12 are from the PTE
        Bits 11:0 are from the original linear address
        '''
        return (pte & 0xffffffffff000) | (vaddr & 0xfff)

    
    def vtop(self, vaddr):
        '''
        Translates virtual addresses into physical offsets.
        The function returns either None (no valid mapping)
        or the offset in physical memory where the address maps.
        '''
        pdpte = self.get_pdpte(vaddr)
        if not self.entry_present(pdpte):
            # Add support for paged out PDPTE
            # Insert buffalo here!
            return None

        pde = self.get_pde(vaddr, pdpte)
        if not self.entry_present(pde):
            # Add support for paged out PDE
            return None

        if self.page_size_flag(pde):
            return self.get_two_meg_paddr(vaddr, pde)

        pte = self.get_pte(vaddr, pde)
        if not self.entry_present(pte):
            # Add support for paged out PTE
            return None

        return self.get_phys_addr(vaddr, pte)

    def read_long_long_phys(self, addr):
        '''
        Returns an unsigned 64-bit integer from the address addr in
        physical memory. If unable to read from that location, returns None.
        '''
        string = self.base.read(addr, 8)
        if not string:
            return None
        (longlongval, ) = struct.unpack('<Q', string)
        return longlongval

    def get_available_pages(self, start=0):
        '''
        Return a list of lists of available memory pages.
        Each entry in the list is the starting virtual address 
        and the size of the memory page.
        '''

        # Pages that hold PDEs and PTEs are 0x1000 bytes each.
        # Each PDE and PTE is eight bytes. Thus there are 0x1000 / 8 = 0x200
        # PDEs and PTEs we must test.

        page_list = []
        start_pdpte = self.pdpte_index(start)
        for pdpte in range(start_pdpte, 4):
            vaddr = pdpte << 30
            pdpte_value = self.get_pdpte(vaddr)
            if not self.entry_present(pdpte_value):
                continue
            for pde in range(0, 0x200):
                vaddr = pdpte << 30 | (pde << 21)
                pde_value = self.get_pde(vaddr, pdpte_value)
                if not self.entry_present(pde_value):
                    continue
                if self.page_size_flag(pde_value):
                    yield (vaddr, 0x200000)
                    continue

                tmp = vaddr
                for pte in range(0, 0x200):
                    vaddr = tmp | (pte << 12)
                    pte_value = self.get_pte(vaddr, pde_value)
                    if self.entry_present(pte_value):
                        yield (vaddr, 0x1000)
