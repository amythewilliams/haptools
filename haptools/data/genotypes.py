from __future__ import annotations
from pathlib import Path
from typing import Iterator
from collections import namedtuple

import numpy as np
from cyvcf2 import VCF, Variant

from .data import Data


class Genotypes(Data):
    """
    A class for processing genotypes from a file

    Attributes
    ----------
    data : np.array
        The genotypes in an n (samples) x p (variants) x 2 (strands) array
    fname : Path
        The path to the read-only file containing the data
    samples : tuple[str]
        The names of each of the n samples
    variants : list
        Variant-level meta information:
            1. ID
            2. CHROM
            3. POS
            4. AAF: allele freq of alternate allele (or MAF if to_MAC() is called)
    log: Logger
        A logging instance for recording debug statements.

    Examples
    --------
    >>> genotypes = Genotypes.load('tests/data/simple.vcf')
    """

    def __init__(self, fname: Path, log: Logger = None):
        super().__init__(fname, log)
        self.samples = tuple()
        self.variants = np.array([])

    @classmethod
    def load(
        cls: Genotypes, fname: Path, region: str = None, samples: list[str] = None
    ) -> Genotypes:
        """
        Load genotypes from a VCF file

        Read the file contents, check the genotype phase, and create the MAC matrix

        Parameters
        ----------
        fname
            See documentation for :py:attr:`~.Data.fname`
        region : str, optional
            See documentation for :py:meth:`~.Genotypes.read`
        samples : list[str], optional
            See documentation for :py:meth:`~.Genotypes.read`

        Returns
        -------
        genotypes
            A Genotypes object with the data loaded into its properties
        """
        genotypes = cls(fname)
        genotypes.read(region, samples)
        genotypes.check_biallelic()
        genotypes.check_phase()
        # genotypes.to_MAC()
        return genotypes

    def read(self, region: str = None, samples: list[str] = None):
        """
        Read genotypes from a VCF into a numpy matrix stored in :py:attr:`~.Genotypes.data`

        Raises
        ------
        ValueError
            If the genotypes array is empty

        Parameters
        ----------
        region : str, optional
            The region from which to extract genotypes; ex: 'chr1:1234-34566' or 'chr7'

            For this to work, the VCF must be indexed and the seqname must match!

            Defaults to loading all genotypes
        samples : list[str], optional
            A subset of the samples from which to extract genotypes

            Defaults to loading genotypes from all samples
        """
        super().read()
        # initialize variables
        vcf = VCF(str(self.fname), samples=samples)
        self.samples = tuple(vcf.samples)
        self.variants = []
        self.data = []
        # load all info into memory
        for variant in vcf(region):
            # save meta information about each variant
            self.variants.append((variant.ID, variant.CHROM, variant.POS, variant.aaf))
            # extract the genotypes to a matrix of size n x p x 3
            # the last dimension has three items:
            # 1) presence of REF in strand one
            # 2) presence of REF in strand two
            # 3) whether the genotype is phased
            self.data.append(variant.genotypes)
        vcf.close()
        # convert to np array for speedy operations later on
        self.variants = np.array(
            self.variants,
            dtype=[
                ("id", "U50"),
                ("chrom", "U10"),
                ("pos", np.uint),
                ("aaf", np.float64),
            ],
        )
        self.data = np.array(self.data, dtype=np.uint8)
        if self.data.shape == (0, 0, 0):
            self.log.warning(
                "Failed to load genotypes. If you specified a region, check that the"
                " contig name matches! For example, double-check the 'chr' prefix."
            )
        # transpose the GT matrix so that samples are rows and variants are columns
        self.data = self.data.transpose((1, 0, 2))

    def iterate(
        self, region: str = None, samples: list[str] = None
    ) -> Iterator[namedtuple]:
        """
        Read genotypes from a VCF line by line without storing anything

        Parameters
        ----------
        region : str, optional
            The region from which to extract genotypes; ex: 'chr1:1234-34566' or 'chr7'

            For this to work, the VCF must be indexed and the seqname must match!

            Defaults to loading all genotypes
        samples : list[str], optional
            A subset of the samples from which to extract genotypes

            Defaults to loading genotypes from all samples

        Yields
        ------
        Iterator[namedtuple]
            An iterator over each line in the file, where each line is encoded as a
            namedtuple containing each of the class properties
        """
        vcf = VCF(str(self.fname), samples=samples)
        samples = tuple(vcf.samples)
        Record = namedtuple("Record", "data samples variants")
        # load all info into memory
        for variant in vcf(region):
            # save meta information about each variant
            variants = np.array(
                (variant.ID, variant.CHROM, variant.POS, variant.aaf),
                dtype=[
                    ("id", "U50"),
                    ("chrom", "U10"),
                    ("pos", np.uint),
                    ("aaf", np.float64),
                ],
            )
            # extract the genotypes to a matrix of size 1 x p x 3
            # the last dimension has three items:
            # 1) presence of REF in strand one
            # 2) presence of REF in strand two
            # 3) whether the genotype is phased
            data = np.array(variant.genotypes, dtype=np.uint8)
            yield Record(data, samples, variants)
        vcf.close()

    def check_biallelic(self, discard_also=False):
        """
        Check that each genotype is composed of only two alleles

        This function modifies the dtype of :py:attr:`~.Genotypes.data` from uint8 to bool

        Raises
        ------
        AssertionError
            If the number of alleles has already been checked and the dtype has been
            converted to bool
        ValueError
            If any of the genotypes have more than two alleles

        Parameters
        ----------
        discard_also : bool, optional
            If True, discard any multiallelic variants without raising a ValueError
        """
        if self.data.dtype == np.bool_:
            self.log.warning("All genotypes are already biallelic")
            return
        # check: are there any variants that have genotype values above 1?
        # A genotype value above 1 would imply the variant has more than one ALT allele
        multiallelic = np.any(self.data[:, :, :2] > 1, axis=2)
        if np.any(multiallelic):
            samp_idx, variant_idx = np.nonzero(multiallelic)
            if discard_also:
                self.data = np.delete(self.data, variant_idx, axis=1)
                self.variants = np.delete(self.variants, variant_idx)
            else:
                raise ValueError(
                    "Variant with ID {} at POS {}:{} is multiallelic for sample {}".format(
                        *tuple(self.variants[variant_idx[0]])[:3],
                        self.samples[samp_idx[0]],
                    )
                )
        self.data = self.data.astype(np.bool_)

    def check_phase(self):
        """
        Check that the genotypes are phased then remove the phasing info from the data

        This function modifies :py:attr:`~.Genotypes.data` in-place

        Raises
        ------
        AssertionError
            If the phase information has already been checked and removed from the data
        ValueError
            If any heterozgyous genotpyes are unphased
        """
        if self.data.shape[2] < 3:
            self.log.warning("Phase information has already been removed from the data")
            return
        # check: are there any variants that are heterozygous and unphased?
        unphased = (self.data[:, :, 0] ^ self.data[:, :, 1]) & (~self.data[:, :, 2])
        if np.any(unphased):
            samp_idx, variant_idx = np.nonzero(unphased)
            raise ValueError(
                "Variant with ID {} at POS {}:{} is unphased for sample {}".format(
                    *tuple(self.variants[variant_idx[0]])[:3], self.samples[samp_idx[0]]
                )
            )
        # remove the last dimension that contains the phase info
        self.data = self.data[:, :, :2]

    def to_MAC(self):
        """
        Convert the ALT count GT matrix into a matrix of minor allele counts

        This function modifies :py:attr:`~.Genotypes.data` in-place

        It also changes the 'aaf' record in :py:attr:`~.Genotypes.variants` to 'maf'

        Raises
        ------
        AssertionError
            If the matrix has already been converted
        """
        if self.variants.dtype.names[3] == "maf":
            self.log.warning(
                "The matrix already counts instances of the minor allele rather than "
                "the ALT allele."
            )
            return
        need_conversion = self.variants["aaf"] > 0.5
        # flip the count on the variants that have an alternate allele frequency
        # above 0.5
        self.data[:, need_conversion, :2] = ~self.data[:, need_conversion, :2]
        # also encode an MAF instead of an AAF in self.variants
        self.variants["aaf"][need_conversion] = (
            1 - self.variants["aaf"][need_conversion]
        )
        # replace 'aaf' with 'maf' in the matrix
        self.variants.dtype.names = [
            (x, "maf")[x == "aaf"] for x in self.variants.dtype.names
        ]


class GenotypesPLINK(Data):
    """
    A class for processing genotypes from a PLINK .pgen file

    Attributes
    ----------
    data : np.array
        The genotypes in an n (samples) x p (variants) x 2 (strands) array
    samples : tuple
        The names of each of the n samples
    variants : list
        Variant-level meta information:
            1. ID
            2. CHROM
            3. POS
            4. AAF: allele freq of alternate allele (or MAF if to_MAC() is called)
    log: Logger
        A logging instance for recording debug statements.

    Examples
    --------
    >>> genotypes = Genotypes.load('tests/data/simple.pgen')
    """

    def __init__(self, fname: Path, log: Logger = None):
        super().__init__(fname, log)
        self.samples = tuple()
        self.variants = np.array([])

    @classmethod
    def load(
        cls: Genotypes, fname: Path, region: str = None, samples: list[str] = None
    ) -> Genotypes:
        """
        Load genotypes from a VCF file

        Read the file contents, check the genotype phase, and create the MAC matrix

        Parameters
        ----------
        fname
            See documentation for :py:attr:`~.Data.fname`
        region : str, optional
            See documentation for :py:meth:`~.Genotypes.read`
        samples : list[str], optional
            See documentation for :py:meth:`~.Genotypes.read`

        Returns
        -------
        genotypes
            A Genotypes object with the data loaded into its properties
        """
        genotypes = cls(fname)
        genotypes.read(region, samples)
        genotypes.check_biallelic()
        genotypes.check_phase()
        # genotypes.to_MAC()
        return genotypes

    def read(self, region: str = None, samples: list[str] = None):
        """
        Read genotypes from a VCF into a numpy matrix stored in :py:attr:`~.Genotypes.data`

        Parameters
        ----------
        region : str, optional
            The region from which to extract genotypes; ex: 'chr1:1234-34566' or 'chr7'

            For this to work, the VCF must be indexed and the seqname must match!

            Defaults to loading all genotypes
        samples : list[str], optional
            A subset of the samples from which to extract genotypes

            Defaults to loading genotypes from all samples
        """
        super().read()
        # TODO: load the variant-level info from the .pvar file
        # and use that info to figure out how many variants there are in the region
        variant_ct_start = 0
        variant_ct_end = None
        variant_ct = variant_ct_end - variant_ct_start
        # load the pgen-reader file
        # note: very little is loaded into memory at this point
        from pgenlib import PgenReader  # TODO: figure out how to install this package

        pgen = PgenReader(bytes(self.fname))
        sample_ct = pgen.get_raw_sample_ct()
        # the genotypes start out as a simple 2D array with twice the number of samples
        # so each column is a different chromosomal strand
        self.data = np.empty((variant_ct, sample_ct * 2), np.int32)
        pgen.read_alleles_range(variant_ct_start, variant_ct_end, self.data)
        # extract the genotypes to a np matrix of size n x p x 2
        # the last dimension has two items:
        # 1) presence of REF in strand one
        # 2) presence of REF in strand two
        self.data = np.dstack((self.data[:, ::2], self.data[:, 1::2]))
        # transpose the GT matrix so that samples are rows and variants are columns
        self.data = self.data.transpose((1, 0, 2))